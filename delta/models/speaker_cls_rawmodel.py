# Copyright (C) 2017 Beijing Didi Infinity Technology and Development Co.,Ltd.
# All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
'''
A series of models for speaker classification.
'''
import math
from absl import logging
import tensorflow as tf
import tensorflow.keras.layers as keras_layers

from delta import utils
from delta.layers import common_layers
from delta.models.base_model import RawModel
from delta.utils.register import registers

#pylint: disable=invalid-name
#pylint: disable=too-many-locals
#pylint: disable=too-many-instance-attributes
#pylint: disable=arguments-differ


class SpeakerBaseRawModel(RawModel):
  '''
  Base class for speaker models.
  '''

  def __init__(self, config, name=None):
    super().__init__(name=name)
    self.config = config

    self.netconf = self.config['model']['net']['structure']
    self.taskconf = self.config['data']['task']
    self.audioconf = self.taskconf['audio']

    self.attention = self.netconf['attention']
    self.vocab_size = self.taskconf['text']['vocab_size']
    frame_per_sec = 1 / self.taskconf['audio']['winstep']
    self.input_len = self.taskconf['audio']['clip_size'] * frame_per_sec
    self.input_type = 'samples' if self.taskconf[
        'suffix'] == '.wav' else 'features'
    self.input_channels = 3 if self.taskconf['audio']['add_delta_deltas'] else 1

    # l2
    self._extra_train_ops = []

    # internal parameters
    self.feature_params = None
    self.mean = None
    self.std = None
    self.train = None

  def preprocess(self, inputs):
    ''' Speech preprocessing. '''
    with tf.variable_scope('feature'):
      if self.input_type == 'samples':
        # FIXME: stub
        feats = None
      else:
        if 'cmvn_type' in self.audioconf:
          cmvn_type = self.audioconf['cmvn_type']
        else:
          cmvn_type = 'global'
        logging.info('cmvn_type: %s' % (cmvn_type))
        if cmvn_type == 'global':
          self.mean, self.std = utils.load_cmvn(self.audioconf['cmvn_path'])
          feats = utils.apply_cmvn(inputs, self.mean, self.std)
        elif cmvn_type == 'local':
          feats = utils.apply_local_cmvn(inputs)
        elif cmvn_type == 'sliding':
          raise ValueError('cmvn_type %s not implemented yet.' % (cmvn_type))
        elif cmvn_type == 'none':
          feats = inputs
        else:
          raise ValueError('Error cmvn_type %s.' % (cmvn_type))
    return feats

  def call(self, features, **kwargs):
    ''' Implementation of __call__(). '''
    self.train = kwargs['training']
    feats = tf.identity(features['inputs'], name='feats')
    logging.info(features)
    if 'labels' in features:
      labels = features['labels']
    else:
      # serving export mode
      labels = None

    with tf.variable_scope('model', reuse=tf.AUTO_REUSE):
      feats = self.preprocess(feats)
      logits = self.model(feats, labels)
    return logits

  def model(self, feats, labels):
    ''' Stub function. '''
    return NotImplementedError('Stub function.')

  def linear_block(self, x):
    '''
    linear layer for dim reduction
    x: shape [batch, time, feat, channel]
    output: shape [b, t, f]
    '''
    times_t = tf.shape(x)[1]
    feat, channel = x.shape.as_list()[2:]
    linear_num = self.netconf['linear_num']
    if linear_num > 0:
      with tf.variable_scope('linear'):
        x = tf.reshape(x, [-1, feat * channel])
        if self.netconf['use_dropout']:
          x = tf.layers.dropout(
              x, self.netconf['dropout_rate'], training=self.train)
        x = common_layers.linear(x, 'linear1', [feat * channel, linear_num])
        x = tf.nn.relu(x)
        if self.netconf['use_bn']:
          bn_name = 'bn_linear'
          x = tf.layers.batch_normalization(
              x, axis=-1, momentum=0.9, training=self.train, name=bn_name)
    else:
      logging.info('linear_num <= 0, only apply reshape.')
      x = tf.reshape(x, [-1, times_t, feat * channel])
    return x

  def lstm_layer(self, x):
    ''' LSTM layers. '''
    if self.netconf['use_lstm_layer']:
      with tf.variable_scope('lstm'):
        cell_fw = tf.contrib.rnn.BasicLSTMCell(
            self.netconf['cell_num'], forget_bias=1.0)
        if self.netconf['use_dropout']:
          cell_fw = tf.contrib.rnn.DropoutWrapper(
              cell=cell_fw,
              output_keep_prob=1 -
              self.netconf['dropout_rate'] if self.train else 1.0)

        cell_bw = tf.contrib.rnn.BasicLSTMCell(
            self.netconf['cell_num'], forget_bias=1.0)
        if self.netconf['use_dropout']:
          cell_bw = tf.contrib.rnn.DropoutWrapper(
              cell=cell_bw,
              output_keep_prob=1 -
              self.netconf['dropout_rate'] if self.train else 1.0)

        # Now we feed `linear` into the LSTM BRNN cell and obtain the LSTM BRNN output.
        outputs, _ = tf.nn.bidirectional_dynamic_rnn(
            cell_fw=cell_fw,
            cell_bw=cell_bw,
            inputs=x,
            dtype=tf.float32,
            time_major=False,
            scope='LSTM1')
    else:
      outputs = x
    return outputs

  def pooling_layer(self, x):
    '''
      Add a pooling layer across the whole utterance.
      Input: [NHW]
        --> Reduce along H

      Statistics pooling output: [N, W * 2]
      Average pooling output: [N, W]
    '''
    pooling_type = self.netconf['frame_pooling_type']
    if pooling_type == 'stats':
      with tf.variable_scope('stats_pooling'):
        mean, var = tf.nn.moments(x, 1)
        x = tf.concat([mean, tf.sqrt(var + 1e-6)], 1)
    elif pooling_type == 'average':
      with tf.variable_scope('average_pooling'):
        mean, _ = tf.nn.moments(x, 1)
        x = mean
    else:
      raise ValueError('Unsupported frame_pooling_type: %s' % (pooling_type))
    return x

  def dense_layer(self, x):
    ''' Embedding layers. '''
    with tf.variable_scope('dense'):
      shape = x.shape[-1].value
      hidden_dims = self.netconf['hidden_dims']
      y = x
      use_bn = self.netconf['use_bn']
      remove_nonlin = self.netconf['remove_last_nonlinearity']
      for idx, hidden in enumerate(hidden_dims):
        last_layer = idx == (len(hidden_dims) - 1)
        y = common_layers.linear(
            y,
            'dense-matmul-%d' % (idx + 1), [shape, hidden],
            has_bias=not use_bn)
        shape = hidden
        embedding = y
        if not last_layer or not remove_nonlin:
          y = tf.nn.relu(y)
        if use_bn:
          y = tf.layers.batch_normalization(
              y,
              axis=-1,
              momentum=0.99,
              training=self.train,
              name='dense-bn-%d' % (idx + 1))
        if self.netconf['use_dropout'] and not remove_nonlin:
          y = tf.layers.dropout(
              y, self.netconf['dropout_rate'], training=self.train)
      if self.netconf['embedding_after_linear']:
        logging.info('Output embedding right after linear layer.')
      else:
        logging.info('Output embedding after non-lin, batch norm and dropout.')
        embedding = y
    return embedding, y

  def arcface_layer(self, inputs, labels, output_num, weights):
    ''' ArcFace layer. '''

    def arcface_loss(embedding,
                     labels,
                     out_num,
                     weights,
                     s=64.,
                     m=0.5,
                     limit_to_pi=True):
      '''
      https://github.com/auroua/InsightFace_TF/blob/master/losses/face_losses.py
      :param embedding: the input embedding vectors
      :param labels:  the input labels, the shape should be eg: (batch_size, 1)
      :param s: scalar value default is 64
      :param out_num: output class num
      :param m: the margin value, default is 0.5
      :return: the final cacualted output, this output is send into the tf.nn.softmax directly
      '''
      cos_m = math.cos(m)
      sin_m = math.sin(m)
      mm = sin_m * m  # issue 1
      threshold = math.cos(math.pi - m)
      with tf.variable_scope('arcface_loss'):
        # inputs and weights norm
        embedding_norm = tf.norm(embedding, axis=1, keep_dims=True)
        embedding = tf.div(embedding, embedding_norm, name='norm_embedding')
        weights_norm = tf.norm(weights, axis=0, keep_dims=True)
        weights = tf.div(weights, weights_norm, name='norm_weights')
        # cos(theta+m)
        cos_t = tf.matmul(embedding, weights, name='cos_t')
        cos_t2 = tf.square(cos_t, name='cos_2')
        sin_t2 = tf.subtract(1., cos_t2, name='sin_2')
        sin_t = tf.sqrt(sin_t2, name='sin_t')
        cos_mt = s * tf.subtract(
            tf.multiply(cos_t, cos_m), tf.multiply(sin_t, sin_m), name='cos_mt')

        if limit_to_pi:
          # this condition controls the theta+m should in range [0, pi]
          #      0<=theta+m<=pi
          #     -m<=theta<=pi-m
          cond_v = cos_t - threshold
          cond = tf.cast(tf.nn.relu(cond_v, name='if_else'), dtype=tf.bool)

          keep_val = s * (cos_t - mm)
          cos_mt_temp = tf.where(cond, cos_mt, keep_val)
        else:
          cos_mt_temp = cos_mt

        mask = tf.one_hot(labels, depth=out_num, name='one_hot_mask')
        # mask = tf.squeeze(mask, 1)
        inv_mask = tf.subtract(1., mask, name='inverse_mask')

        s_cos_t = tf.multiply(s, cos_t, name='scalar_cos_t')

        output = tf.add(
            tf.multiply(s_cos_t, inv_mask),
            tf.multiply(cos_mt_temp, mask),
            name='arcface_loss_output')
      return output

    params = self.netconf['arcface_params']
    s = params['scale']
    m = params['margin']
    limit_to_pi = params['limit_to_pi']
    return arcface_loss(
        inputs, labels, output_num, weights, s=s, m=m, limit_to_pi=limit_to_pi)

  def logits_layer(self, x, labels):
    ''' Logits layer to further produce softmax. '''
    if labels is None:
      # serving export mode, no need for logits
      return x
    output_num = self.taskconf['classes']['num']
    logits_type = self.netconf['logits_type']
    logits_shape = [x.shape[-1].value, output_num]
    with tf.variable_scope('logits'):
      init_type = self.netconf['logits_weight_init']['type']
      if init_type == 'truncated_normal':
        stddev = self.netconf['logits_weight_init']['stddev']
        init = tf.truncated_normal_initializer(stddev=stddev)
      elif init_type == 'xavier_uniform':
        init = tf.contrib.layers.xavier_initializer(uniform=True)
      elif init_type == 'xavier_norm':
        init = tf.contrib.layers.xavier_initializer(uniform=False)
      else:
        raise ValueError('Unsupported weight init type: %s' % (init_type))
      weights = tf.get_variable(
          name='weights',
          shape=logits_shape,
          initializer=init)
      if logits_type == 'linear':
        bias = tf.get_variable(
            name='bias',
            shape=logits_shape[1],
            initializer=tf.constant_initializer(0.0))
        return tf.matmul(x, weights) + bias
      elif logits_type == 'linear_no_bias':
        return tf.matmul(x, weights)
      elif logits_type == 'arcface':
        return self.arcface_layer(x, labels, output_num, weights)


@registers.model.register
class SpeakerCRNNRawModel(SpeakerBaseRawModel):
  ''' A speaker model with simple 2D conv layers. '''

  def model(self, feats, labels):
    ''' Build the model. '''
    x, _ = self.conv_block(feats, depthwise=False)
    x = self.linear_block(x)
    x = self.lstm_layer(x)
    x = self.pooling_layer(x)
    embedding, dense_output = self.dense_layer(x)
    logits = self.logits_layer(dense_output, labels)
    model_outputs = {'logits': logits, 'embeddings': embedding}
    return model_outputs

  def conv_block(self, inputs, depthwise=False):
    ''' 2D conv layers. '''
    filters = self.netconf['filters']
    logging.info("filters : {}".format(filters))
    filters_size = self.netconf['filter_size']
    logging.info("filters_size : {}".format(filters_size))
    filters_strides = self.netconf['filter_stride']
    logging.info("filters_strides : {}".format(filters_strides))
    pools_size = self.netconf['pool_size']
    logging.info("pools_size : {}".format(pools_size))

    layer_num = len(filters)
    assert layer_num == len(filters_size)
    assert layer_num == len(filters_strides)
    assert layer_num == len(pools_size)

    channels = [self.input_channels] + filters
    logging.info("channels : {}".format(channels))

    downsample_input_len = self.input_len
    with tf.variable_scope('cnn'):
      x = tf.identity(inputs)
      for index, filt in enumerate(filters):
        unit_name = 'unit-' + str(index + 1)
        with tf.variable_scope(unit_name):
          if depthwise:
            x = tf.layers.separable_conv2d(
                x,
                filters=filt,
                kernel_size=filters_size[index],
                strides=filters_strides[index],
                padding='same',
                name=unit_name)
          else:
            cnn_name = 'cnn-' + str(index + 1)
            x = common_layers.conv2d(x, cnn_name, filters_size[index],
                                     channels[index], channels[index + 1],
                                     filters_strides[index])
          x = tf.nn.relu(x)
          if self.netconf['use_bn']:
            bn_name = 'bn' + str(index + 1)
            x = tf.layers.batch_normalization(
                x, axis=-1, momentum=0.9, training=self.train, name=bn_name)
          if self.netconf['use_dropout']:
            x = tf.layers.dropout(
                x, self.netconf['dropout_rate'], training=self.train)
          x = common_layers.max_pool(x, pools_size[index], pools_size[index])
          downsample_input_len = downsample_input_len / pools_size[index][0]

    return x, downsample_input_len


@registers.model.register
class SpeakerTDNNRawModel(SpeakerBaseRawModel):
  ''' A speaker model with TDNN layers. '''

  def model(self, feats, labels):
    ''' Build the model. '''
    x, _ = self.tdnn_block(feats)
    x = self.pooling_layer(x)
    embedding, dense_output = self.dense_layer(x)
    logits = self.logits_layer(dense_output, labels)
    model_outputs = {'logits': logits, 'embeddings': embedding}
    return model_outputs

  def tdnn_block(self, inputs):
    ''' TDNN layers. '''
    if 'tdnn_method' in self.netconf:
      tdnn_method = self.netconf['tdnn_method']
    else:
      # Runs faster, support discrete context, for now.
      tdnn_method = 'splice_layer'
    tdnn_contexts = self.netconf['tdnn_contexts']
    logging.info("tdnn_contexts : {}".format(tdnn_contexts))
    tdnn_dims = self.netconf['tdnn_dims']
    logging.info("tdnn_dims : {}".format(tdnn_dims))

    layer_num = len(tdnn_contexts)
    assert layer_num == len(tdnn_dims)

    channels = [self.input_channels] + tdnn_dims
    logging.info("tdnn_channels : {}".format(channels))

    input_h_t = tf.shape(inputs)[1]
    input_w = inputs.shape[2]
    input_c = inputs.shape[3]
    if tdnn_method == 'conv1d':
      # NHWC -> NW'C, W' = H * W
      inputs = tf.reshape(inputs, [-1, input_h_t * input_w, input_c])
      last_w = channels[0]
    else:
      inputs = tf.reshape(inputs, [-1, input_h_t, input_w * input_c])
      last_w = input_w * input_c

    downsample_input_len = self.input_len
    with tf.variable_scope('tdnn'):
      x = tf.identity(inputs)
      for index in range(layer_num):
        unit_name = 'unit-' + str(index + 1)
        with tf.variable_scope(unit_name):
          tdnn_name = 'tdnn-' + str(index + 1)
          use_bn = self.netconf['use_bn']
          has_bias = not use_bn
          x = common_layers.tdnn(
              x,
              tdnn_name,
              last_w,
              tdnn_contexts[index],
              channels[index + 1],
              has_bias=has_bias,
              method=tdnn_method)
          last_w = channels[index + 1]
          x = tf.nn.relu(x)
          if self.netconf['use_bn']:
            bn_name = 'bn' + str(index + 1)
            x = tf.layers.batch_normalization(
                x, axis=-1, momentum=0.9, training=self.train, name=bn_name)
          if self.netconf['use_dropout']:
            x = tf.layers.dropout(
                x, self.netconf['dropout_rate'], training=self.train)
          downsample_input_len = downsample_input_len

    return x, downsample_input_len


@registers.model.register
class SpeakerResNetRawModel(SpeakerBaseRawModel):
  ''' A speaker model with ResNet layers. '''

  def model(self, feats, labels):
    ''' Build the model. '''
    x = self.resnet(feats)
    x = self.linear_block(x)
    x = self.pooling_layer(x)
    embedding, dense_output = self.dense_layer(x)
    logits = self.logits_layer(dense_output, labels)
    model_outputs = {'logits': logits, 'embeddings': embedding}
    return model_outputs

  def bn_layer(self, x, bn_name):
    x = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name)
    return x

  def prelu_layer(self, x, name):
    alpha = tf.get_variable(
        name,
        shape=x.get_shape()[-1],
        dtype=x.dtype,
        initializer=tf.constant_initializer(0.1))
    return tf.maximum(0.0, x) + alpha * tf.minimum(0.0, x)

  def se_moudle(self, x, channels, reduction, name=''):
    input = x
    x = tf.reduce_mean(x, [1, 2], name=name + '_avg', keep_dims=True)
    x = tf.layers.conv2d(
        x,
        channels // reduction, (1, 1),
        use_bias=True,
        name=name + '_1x1_down',
        strides=(1, 1),
        padding='valid',
        data_format='channels_last',
        activation=None,
        kernel_initializer=tf.contrib.layers.xavier_initializer(),
        bias_initializer=tf.zeros_initializer())
    x = tf.nn.relu(x, name=name + '_1x1_down_relu')

    x = tf.layers.conv2d(
        x,
        channels, (1, 1),
        use_bias=True,
        name=name + '_1x1_up',
        strides=(1, 1),
        padding='valid',
        data_format='channels_last',
        activation=None,
        kernel_initializer=tf.contrib.layers.xavier_initializer(),
        bias_initializer=tf.zeros_initializer())
    x = tf.nn.sigmoid(x, name=name + '_1x1_up_sigmoid')
    return tf.multiply(input, x, name=name + '_mul')

  def resnet_layer(self, x, in_channel, out_channel, stride, dim_match,
                   block_name):
    conv_name_base = 'res' + block_name + '_branch'
    bn_name_base = 'bn' + block_name + '_branch'
    prelu_name_base = 'prelu' + block_name + '_branch'

    short_cut = x
    if not dim_match:
      short_cut = common_layers.conv2d(short_cut, conv_name_base + '1', (1, 1),
                                       in_channel, out_channel, stride)
      short_cut = tf.layers.batch_normalization(
          short_cut,
          axis=-1,
          momentum=0.9,
          training=self.train,
          name=bn_name_base + '1')
    x = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name_base + '2a')
    x = common_layers.conv2d(x, conv_name_base + '2a', (3, 3), in_channel,
                             out_channel, [1, 1])
    x = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name_base + '2b')
    x = self.prelu_layer(x, name=prelu_name_base + '2b')
    x = common_layers.conv2d(x, conv_name_base + '2b', (3, 3), out_channel,
                             out_channel, stride)
    res = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name_base + '2c')

    return tf.add(short_cut, res, name='add_' + block_name)

  def se_resnet_layer(self, x, in_channel, out_channel, stride, dim_match,
                      block_name):
    conv_name_base = 'res_' + block_name + '_branch'
    bn_name_base = 'bn_' + block_name + '_branch'
    prelu_name_base = 'prelu_' + block_name + '_branch'
    se_name_base = 'se_' + block_name + '_branch'

    short_cut = x
    if not dim_match:
      short_cut = common_layers.conv2d(short_cut, conv_name_base + '1', (1, 1),
                                       in_channel, out_channel, stride)
      short_cut = tf.layers.batch_normalization(
          short_cut,
          axis=-1,
          momentum=0.9,
          training=self.train,
          name=bn_name_base + '1')
    x = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name_base + '2a')
    x = common_layers.conv2d(x, conv_name_base + '2a', (3, 3), in_channel,
                             out_channel, [1, 1])
    x = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name_base + '2b')
    x = self.prelu_layer(x, name=prelu_name_base + '2b')
    x = common_layers.conv2d(x, conv_name_base + '2b', (3, 3), out_channel,
                             out_channel, stride)
    x = tf.layers.batch_normalization(
        x, axis=-1, momentum=0.9, training=self.train, name=bn_name_base + '2c')
    res = self.se_moudle(x, out_channel, 16, name=se_name_base)

    return tf.add(short_cut, res, name='add_' + block_name)

  def resnet_block(self, x, block_mode, layer_num, in_channel, out_channel,
                   stride):
    if block_mode == 'ir':
      block = self.resnet_layer
    elif block_mode == 'ir_se':
      block = self.se_resnet_layer

    x = block(x, in_channel, out_channel, stride, False, block_name='a')
    for i in range(1, layer_num):
      x = block(
          x,
          out_channel,
          out_channel, [1, 1],
          True,
          block_name=chr(ord('a') + i))

    return x

  def resnet(self, inputs):
    ''' resnet_block. '''
    layers_list = self.netconf['layers_list']
    logging.info("layers_list : {}".format(layers_list))
    filters_list = self.netconf['filters_list']
    logging.info("filters_list : {}".format(filters_list))
    strides_list = self.netconf['strides_list']
    logging.info("strides_list : {}".format(strides_list))
    block_mode = self.netconf['block_mode']
    logging.info("block_mode : {}".format(block_mode))

    with tf.variable_scope('resnet'):
      x = tf.identity(inputs)
      with tf.variable_scope('input_layer'):
        x = common_layers.conv2d(x, 'input_conv', (3, 3), self.input_channels,
                                 filters_list[0], [1, 1])
        x = tf.layers.batch_normalization(
            x, axis=-1, momentum=0.9, training=self.train, name='input_bn')
        x = self.prelu_layer(x, 'input_prelu')

      for index, layer_num in enumerate(layers_list):
        unit_name = 'resblock-' + str(index + 1)
        with tf.variable_scope(unit_name):
          x = self.resnet_block(x, block_mode, layer_num, filters_list[index],
                                filters_list[index + 1], strides_list[index])

    return x
