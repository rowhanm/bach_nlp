import os
import pathlib
import sys
import matplotlib.pyplot as plt

import feature_extractors
import scoring
from thrush_attention import AttentionLayer

import numpy as np
import random

import tensorflow as tf
from tensorflow import keras
K = keras.backend

LATENT_DIM = int(sys.argv[2])

COMPONENTS_IN_ORDER = ['key', 'mode', 'degree',
                       'inversion', 'quality', 'measure', 'beat', 'is_terminal']

# This would make more sense as a dict of lambdas, but independant statments are
# required for AutoGraph magic in TF.


def KEY_SLICE(x): return x[:, :, :12],
def MODE_SLICE(x): return K.expand_dims(x[:, :, 12]),
def DEGREE_SLICE(x): return x[:, :, 13:21],
def INVERSION_SLICE(x): return x[:, :, 21:25],
def QUALITY_SLICE(x): return x[:, :, 25:30],
def MEASURE_SLICE(x): return K.expand_dims(x[:, :, 30]),
def BEAT_SLICE(x): return x[:, :, 31:33],
def IS_TERMINAL_SLICE(x): return K.expand_dims(x[:, :, 33]),

# This function consumes the outputs of the final dense layer in the model and
# splits its up according to parts of the roman numeral analysis.  This should
# be at the end of the model.


def make_output_components(dense_outputs):
    return {
        'key': keras.layers.Lambda(KEY_SLICE, name='key_l')(dense_outputs)[0],
        'mode': keras.layers.Lambda(MODE_SLICE, name='mode_l')(dense_outputs)[0],
        'degree': keras.layers.Lambda(DEGREE_SLICE, name='degree_l')(dense_outputs)[0],
        'inversion': keras.layers.Lambda(INVERSION_SLICE, name='inversion_l')(dense_outputs)[0],
        'quality': keras.layers.Lambda(QUALITY_SLICE, name='quality_l')(dense_outputs)[0],
        'measure': keras.layers.Lambda(MEASURE_SLICE, name='measure_l')(dense_outputs)[0],
        'beat': keras.layers.Lambda(BEAT_SLICE, name='beat_l')(dense_outputs)[0],
        'is_terminal': keras.layers.Lambda(IS_TERMINAL_SLICE, name='is_terminal_l')(dense_outputs)[0],
    }

# Converts outputs of make_output_components by adding softmax layers to
# appropriate slices


def _convert_dense_to_output_components(dense_outputs):
  # Split out different parts of the dense representation
  output_components = make_output_components(dense_outputs)

  # Apply softmax to dense components modeling probabilities
  for comp in ['key', 'degree', 'inversion', 'quality']:
    output_components[comp] = keras.layers.Softmax(
        name=comp + '_s')(output_components[comp])
  return output_components

# Creates a dictionary for the model's multitask loss, by assigning an
# appropriate loss to each slice of the output.


def create_losses(mask_value):

  xentropy_fn = keras.losses.CategoricalCrossentropy(
      reduction=tf.keras.losses.Reduction.NONE)
  mse_fn = keras.losses.MeanSquaredError(
      reduction=tf.keras.losses.Reduction.NONE)
  # A special loss for is_terminal, which especially penalizes missing the terminal
  # -1.0

  def weighted_loss(weight, loss):
      return lambda x, y: weight*loss(x, y)

  def IsTerminalLoss(y_true, y_pred):
    mse_loss = mse_fn(y_true, y_pred)
    seq_len = tf.reduce_sum(tf.cast(tf.math.logical_not(
        K.all(K.equal(y_true, -1.0), axis=-1)), dtype=tf.float32))
    res = tf.where(tf.squeeze(
        tf.equal(y_true, [-1.0])), mse_loss * seq_len/20, mse_loss)
    return res
  is_terminal_fn = IsTerminalLoss

  def _create_masked_loss(slice_fn, loss_fn):
    def _l(y_true, y_pred):
      mask = tf.math.logical_not(K.all(K.equal(y_true, mask_value), axis=-1))
      y_true = slice_fn(y_true)[0]
      loss = loss_fn(y_true, y_pred)
      masked_loss = tf.boolean_mask(loss, mask)
      avg_masked_loss = tf.reduce_mean(masked_loss)
      return avg_masked_loss
    return _l
  
  LOSSES = {
      'key': _create_masked_loss(KEY_SLICE, weighted_loss(2.5, xentropy_fn)),
      'mode': _create_masked_loss(MODE_SLICE, weighted_loss(1.0, mse_fn)),
      'degree': _create_masked_loss(DEGREE_SLICE, weighted_loss(1.0, xentropy_fn)),
      'inversion': _create_masked_loss(INVERSION_SLICE, weighted_loss(1.0, xentropy_fn)),
      'quality': _create_masked_loss(QUALITY_SLICE, weighted_loss(1.0, xentropy_fn)),
      'measure': _create_masked_loss(MEASURE_SLICE, weighted_loss(1.0, mse_fn)),
      'beat': _create_masked_loss(BEAT_SLICE, weighted_loss(1.0, mse_fn)),
      'is_terminal': _create_masked_loss(IS_TERMINAL_SLICE, weighted_loss(1.0, is_terminal_fn)),
  }
  return LOSSES

# Builds the attention model for training.


def _build_model(constants):
    encoder_inputs = keras.Input(shape=(constants['MAX_CHORALE_LENGTH'], constants['X_DIM']))

    masking_layer = keras.layers.Masking(mask_value=constants['MASK_VALUE'])
    masked_inputs = masking_layer(encoder_inputs)

    encoder = keras.layers.Bidirectional(
        keras.layers.LSTM(LATENT_DIM, return_sequences=True, return_state=True, kernel_regularizer=keras.regularizers.l2(1e-4), recurrent_regularizer=keras.regularizers.l2(1e-4)))
    enc_out, forward_h, forward_c, backward_h, backward_c = encoder(masked_inputs)
    state_h = keras.layers.Concatenate()([forward_h, backward_h])
    state_c = keras.layers.Concatenate()([forward_c, backward_c])
    encoder_states = [state_h, state_c]

    decoder_inputs = keras.Input(
        shape=(constants['MAX_CHORALE_LENGTH'], constants['Y_DIM']))

    decoder_lstm = keras.layers.LSTM(
        LATENT_DIM*2, return_sequences=True, return_state=True)
    dropout_dense = keras.layers.Dropout(0.2)
    decoder_dense = keras.layers.Dense(constants['Y_DIM'], activation=None)

    decoder_outputs, _, _ = decoder_lstm(decoder_inputs,initial_state=encoder_states)
    attn = AttentionLayer(name='thrush_attention')
    attention_outs, attention_energies = attn([enc_out, decoder_outputs])
    drop_outputs = dropout_dense(
        keras.layers.concatenate([decoder_outputs, attention_outs]))
    dense_outputs = decoder_dense(drop_outputs)
    output_components = _convert_dense_to_output_components(dense_outputs)

    m = keras.Model([encoder_inputs, decoder_inputs], output_components)
    return m


class NBatchLogger(keras.callbacks.Callback):
    def __init__(self, display=10):
        '''
        display: Number of batches to wait before outputting loss
        '''
        self.seen = 0
        self.display = display

    def on_epoch_end(self, epoch, logs={}):
        self.seen += epoch
        if self.seen % self.display == 0:
            print('Epoch %s, Metrics: %s' % (str(epoch), str(logs)))


def train():
    train_data, test_data, constants = feature_extractors.load_dataset()
    encoder_input_data, decoder_input_data, decoder_target_data = train_data

    # Add slices here to train on only a subset of the data
    # encoder_input_data = encoder_input_data[:50]
    # decoder_input_data = decoder_input_data[:50]
    # decoder_target_data = decoder_target_data[:50]

    model = _build_model(constants)

    l = create_losses(constants['MASK_VALUE'])
    o = keras.optimizers.Adam(learning_rate=0.005, beta_1=0.8)
    epochs = int(sys.argv[3])
    model.compile(optimizer=o, loss=l)
    model_checkpoint_callback = keras.callbacks.ModelCheckpoint(
        filepath=
        f'thrush_attn_bidir_{LATENT_DIM}',
        save_weights_only=False,
        save_freq=100)
    model.fit([encoder_input_data, decoder_input_data], decoder_target_data,
              batch_size=64,
              epochs=epochs,
              validation_split=0.05,
              shuffle=True,
              callbacks=[model_checkpoint_callback],
              verbose=1)
    model.save(f'thrush_attn_bidir_{LATENT_DIM}_{epochs}')


def predict(epochs):
    train_data, test_data, constants = feature_extractors.load_dataset()
    encoder_input_data, decoder_input_data, decoder_target_data = test_data

    model = keras.models.load_model(f'thrush_attn_bidir_{LATENT_DIM}_{epochs}', custom_objects={
                                    'AttentionLayer': AttentionLayer}, compile=False)

    def _get_layers(layer_type):
      return [l for l in model.layers if layer_type in str(type(l))]
    # Extract encoder from graph
    encoder_inputs = model.input[0]

    enc_outs, state_h_enc_forward, state_c_enc_forward, state_h_enc_backward, state_c_enc_backward = model.layers[2].output
    # return
    state_h_enc = keras.layers.Concatenate()(
        [state_h_enc_forward, state_h_enc_backward])
    state_c_enc = keras.layers.Concatenate()(
        [state_c_enc_forward, state_c_enc_backward])
    encoder_states = [state_h_enc, state_c_enc]
    encoder_model = keras.Model(encoder_inputs, [encoder_states, enc_outs])

    # Extract decoder from graph
    decoder_inputs = keras.Input(
        shape=(1, constants['Y_DIM']), name='rna_input_inference')
    decoder_state_input_h = keras.Input(
        shape=(LATENT_DIM*2,), name="decoder_state_h_inference")
    decoder_state_input_c = keras.Input(
        shape=(LATENT_DIM*2,), name="decoder_state_c_inference")
    decoder_states_inputs = [decoder_state_input_h, decoder_state_input_c]
    encoder_output_input = keras.Input(shape=(
        constants['MAX_CHORALE_LENGTH'], LATENT_DIM*2), name="encoder_output_input")

    decoder_lstm = model.layers[6]
    decoder_attn = model.layers[7]
    decoder_concat = model.layers[8]
    decoder_dense = model.layers[9]

    decoder_outputs, state_h_dec, state_c_dec = decoder_lstm(
        decoder_inputs, initial_state=decoder_states_inputs
    )
    decoder_states = [state_h_dec, state_c_dec]
    attention_outputs, attention_energies = decoder_attn(
        [encoder_output_input, decoder_outputs])
    dense_outputs = decoder_dense(decoder_concat(
        [decoder_outputs, attention_outputs]))
    output_components = _convert_dense_to_output_components(dense_outputs)

    ins = [decoder_inputs]
    ins.extend(decoder_states_inputs)
    ins.append(encoder_output_input)
    outs = [output_components]
    outs.extend(decoder_states)
    outs.append(attention_energies)
    decoder_model = keras.Model(ins, outs)

    # Retruns true when the "is_terminal" output is set to -1, meaning the RNA
    # is finished.
    def _terminate(toks):
        return (toks[-1] < 0)

    # Decode a single chorale.
    def decode(input_seq):
        states_value, encoder_output_values = encoder_model.predict(np.array([input_seq]))
        target_seq = np.ones((1, 1, constants['Y_DIM'])) * -5.

        result = []
        attn_energies = []
        for k in range(constants['MAX_ANALYSIS_LENGTH']):
            ins = [target_seq]
            ins.extend(states_value)
            ins.append(encoder_output_values)
            output_components, h, c, attn_energy = decoder_model.predict(ins)
            attn_energies.append(attn_energy)
            output_tokens = np.concatenate(
                [output_components[key] for key in COMPONENTS_IN_ORDER], axis=-1)
            result.append(output_tokens)
            if _terminate(output_tokens[0][0]):
                return result, attn_energies

            target_seq = np.ones((1, 1, constants['Y_DIM'])) * output_tokens
            states_value = [h, c]
        print("Decoding did not terminate! Returning large RNA.")
        return result, attn_energies

    def cut_off_ground_truth(ground_truth):
        res = []
        for g in ground_truth:
            if _terminate(g):
                return res
            res.append(g)
        print("Ground truth does not terminate! Returning large RNA.")

    err_rates = []
    len_diffs = []
    attn_energy_matrixes = []
    chorale_inds = list(range(len(encoder_input_data)))
    random.shuffle(chorale_inds)
    for chorale_ind in chorale_inds[:20]:
        print("Eval for chorale " + str(chorale_ind))
        # c = encoder_input_data[chorale_ind]
        # c = [i for i in c if i[-1] != -1]
        # print(len(c))


        decoded, attn_energies = decode(encoder_input_data[chorale_ind])
        attn_energy_matrixes.append(attn_energies)

        decoded_rna_chords = [feature_extractors.RNAChord(
            encoding=decoded[i][0][0]) for i in range(len(decoded))]

        ground_truth = cut_off_ground_truth(decoder_target_data[chorale_ind])
        ground_truth_chords = [feature_extractors.RNAChord(
            encoding=ground_truth[i]) for i in range(len(ground_truth))]

        errs = scoring.levenshtein(ground_truth_chords, decoded_rna_chords,
                                   equality_fn=scoring.EQUALITY_FNS['key_enharmonic_and_parallel'], left_deletion_cost=0)
        print(len(ground_truth_chords) - len(decoded_rna_chords))
        len_diffs.append((len(ground_truth_chords) - len(decoded_rna_chords)))
        err_rates.append(float(errs / len(ground_truth_chords)))
        # Uncomment these lines to see the ground truth RNA sequence together
        # with the decoded prediction.
        # print("--------------------- GROUND TRUTH  ------------------")
        # for c in ground_truth_chords:
        #     print(c)
        # print("---------------------  PREDICTION  -------------------")
        # for c in decoded_rna_chords:
        #     print(c)

    print("Error rate: " + str(np.mean(err_rates)))
    print("Len diff: " + str(np.mean(len_diffs)))
    return attn_energy_matrixes


if __name__ == '__main__':
    if sys.argv[1] == "train":
        train()
    elif sys.argv[1] == "pred":
        attns = predict(int(sys.argv[3]))
        mats = []
        dec_inputs = []
        for dec_ind, attn in enumerate(attns[0]):
            mats.append(attn.reshape(-1))
            dec_inputs.append(dec_ind)
        mats = np.array(mats)

        np.save(f'attn_{LATENT_DIM}.npy', mats)
