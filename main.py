import tensorflow as tf
from tensorflow.contrib.layers import batch_norm
from tensorflow.contrib.framework import arg_scope
import numpy as np
import numpy
from data import dataIterator, load_dict, prepare_data
import random
import sys
import copy
import re
import os
import time
import math
import ipdb
import argparse

rng = np.random.RandomState(int(time.time()))

def norm_weight(fan_in, fan_out):
    W_bound = np.sqrt(6.0 / (fan_in + fan_out))
    return np.asarray(rng.uniform(low=-W_bound, high=W_bound, size=(fan_in, fan_out)), dtype=np.float32)

def conv_norm_weight(nin, nout, kernel_size):
    filter_shape = (kernel_size[0], kernel_size[1], nin, nout)
    fan_in = kernel_size[0] * kernel_size[1] * nin
    fan_out = kernel_size[0] * kernel_size[1] * nout
    W_bound = np.sqrt(6. / (fan_in + fan_out))
    W = np.asarray(rng.uniform(low=-W_bound, high=W_bound, size=filter_shape), dtype=np.float32)
    return W.astype('float32')

class Watcher_train():
    def __init__(self, blocks,             # number of dense blocks
                level,                     # number of levels in each blocks
                growth_rate,               # growth rate as mentioned in DenseNet paper: k
                training,
                dropout_rate=0.2,          # Dropout layer's keep-rate
                dense_channels=0,          # Number of filters in transition layer's input
                transition=0.5,            # Compression rate
                input_conv_filters=48,     # Number of filters of conv2d before dense blocks
                input_conv_stride=2,       # Stride of conv2d placed before dense blocks
                input_conv_kernel=[7,7]):  # Size of kernel of conv2d placed before dense blocks
        self.blocks = blocks
        self.level = level
        self.growth_rate = growth_rate
        self.training = training
        self.dense_channels = dense_channels
        self.dropout_rate = dropout_rate
        self.transition = transition
        self.input_conv_filters = input_conv_filters
        self.input_conv_stride = input_conv_stride
        self.input_conv_kernel = input_conv_kernel

    #Bound function for weight initialisation
    def bound(self, nin, nout, kernel):
        kernel_dim_1 = kernel[0]
        kernel_dim_2 = kernel[1]
        mul = kernel_dim_1  * kernel_dim_2
        fin = nin * mul
        fout = nout * mul
        result = (6. / (fin + fout))
        result = np.sqrt(result)
        return result

    def before_dense_net(self,input_x,mask_x):
        #### before flowing into dense blocks ####
        x = input_x
        limit = self.bound(1, self.input_conv_filters, self.input_conv_kernel)
        x = tf.layers.conv2d(x, filters=self.input_conv_filters, strides=self.input_conv_stride,
        kernel_size=self.input_conv_kernel, padding='SAME', data_format='channels_last', use_bias=False, kernel_initializer=tf.random_uniform_initializer(-limit, limit, dtype=tf.float32))
        mask_x = mask_x[:, 0::2, 0::2]
        x = tf.layers.batch_normalization(x, training=self.training, momentum=0.9, scale=True, gamma_initializer=tf.random_uniform_initializer(-1.0/math.sqrt(self.input_conv_filters),
            1.0/math.sqrt(self.input_conv_filters), dtype=tf.float32), epsilon=0.0001)
        x = tf.nn.relu(x)
        x = tf.layers.max_pooling2d(inputs=x, pool_size=[2,2], strides=2, padding='SAME')
        # input_pre = x
        mask_x = mask_x[:, 0::2, 0::2]
        self.dense_channels += self.input_conv_filters
        dense_out = x
        return mask_x , dense_out
    
    def bottleneck(self,x):
        ##---------------------Bottleneck layer to improve computational efficiency,i.e.,to reduce the input to 4k feature maps.(k=24)------------------##
        #### [1, 1] convolution part for bottleneck ####
        filter_size = [1,1]
        limit = self.bound(self.dense_channels, 4 * self.growth_rate, filter_size)
        x = tf.layers.conv2d(x, filters=4 * self.growth_rate, kernel_size=filter_size,
            strides=1, padding='VALID', data_format='channels_last', use_bias=False, kernel_initializer=tf.random_uniform_initializer(-limit, limit, dtype=tf.float32))
        x = tf.layers.batch_normalization(inputs=x,  training=self.training, momentum=0.9, scale=True, gamma_initializer=tf.random_uniform_initializer(-1.0/math.sqrt(4 * self.growth_rate),
            1.0/math.sqrt(4 * self.growth_rate), dtype=tf.float32), epsilon=0.0001)
        x = tf.nn.relu(x)
        x = tf.layers.dropout(inputs=x, rate=self.dropout_rate, training=self.training)
        return x
    
    def convolution_layer_in_DenseB(self,x):
        #### [3, 3] filter for regular convolution layer
        filter_size = [3,3]
        limit = self.bound(4 * self.growth_rate, self.growth_rate,filter_size )
        x = tf.layers.conv2d(x, filters=self.growth_rate, kernel_size=filter_size,
            strides=1, padding='SAME', data_format='channels_last', use_bias=False, kernel_initializer=tf.random_uniform_initializer(-limit, limit, dtype=tf.float32))
        return x
      
    def transition_layer(self,x,mask_x):
        ####There is no transition layer after last DenseB layer,so this module is not run for the last block.####
        compressed_channels = int(self.dense_channels * self.transition)
        #### new dense channels for new dense block ####
        self.dense_channels = compressed_channels
        limit = self.bound(self.dense_channels, compressed_channels, [1,1])
        x = tf.layers.conv2d(x, filters=compressed_channels, kernel_size=[1,1],
            strides=1, padding='VALID', data_format='channels_last', use_bias=False, kernel_initializer=tf.random_uniform_initializer(-limit, limit, dtype=tf.float32))
        x = tf.layers.batch_normalization(x, training=self.training, momentum=0.9, scale=True, gamma_initializer=tf.random_uniform_initializer(-1.0/math.sqrt(self.dense_channels),
                1.0/math.sqrt(self.dense_channels), dtype=tf.float32), epsilon=0.0001)
        x = tf.nn.relu(x)
        x = tf.layers.dropout(inputs=x, rate=self.dropout_rate, training=self.training)
        x = tf.layers.average_pooling2d(inputs=x, pool_size=[2,2], strides=2, padding='SAME')
        dense_out = x
        mask_x = mask_x[:, 0::2, 0::2]
        return x,dense_out,mask_x

    def DenseB_and_transition_layer(self,x,mask_x,dense_out):
        #### flowing into dense blocks and transition_layer ####
        for i in range(self.blocks):
            for j in range(self.level):
                ##----------------------------------------------------------DenseB Layer---------------------------------------------------------------------------##
                #### Bottleneck layer ####
                x = self.bottleneck(x)
                #### 3x3 Convolution Layer ####
                x = self.convolution_layer_in_DenseB(x)
                #### Batch Normalisation Layer ####
                x = tf.layers.batch_normalization(inputs=x, training=self.training, momentum=0.9, scale=True, gamma_initializer=tf.random_uniform_initializer(-1.0/math.sqrt(self.growth_rate),
                    1.0/math.sqrt(self.growth_rate), dtype=tf.float32), epsilon=0.0001)
                #### Relu Activation Layer ####
                x = tf.nn.relu(x)
                x = tf.layers.dropout(inputs=x, rate=self.dropout_rate, training=self.training)
                dense_out = tf.concat([dense_out, x], axis=3)
                x = dense_out
                #### calculate the filter number of dense block's output ####
                self.dense_channels += self.growth_rate

            if i < self.blocks - 1:
                ##---------------------------------------------------------Transition Layer------------------------------------------------------------------------##
                x,dense_out,mask_x = self.transition_layer(x,mask_x)

        return mask_x ,dense_out

    def dense_net(self, input_x, mask_x):
        #### before flowing into dense blocks ####
        x,mask_x ,dense_out = self.before_dense_net(input_x, mask_x)
        #### flowing into dense blocks and transition_layer ####
        mask_x ,dense_out = self.DenseB_and_transition_layer(x,mask_x,dense_out)
        return dense_out, mask_x  
      

class Attender():
    def __init__(self, channels,                                # output of Watcher | [batch, h, w, channels]
                dim_decoder, dim_attend):                       # decoder hidden state:$h_{t-1}$ | [batch, dec_dim]

        self.channels = channels

        self.coverage_kernel = [11,11]                          # kernel size of $Q$
        self.coverage_filters = dim_attend                      # filter numbers of $Q$ | 512

        self.dim_decoder = dim_decoder                          # 256
        self.dim_attend = dim_attend                            # unified dim of three parts calculating $e_ti$ i.e.
                                                                # $Q*beta_t$, $U_a * a_i$, $W_a x h_{t-1}$ | 512

        self.U_f = tf.Variable(norm_weight(self.coverage_filters, self.dim_attend), name='U_f') # $U_f x f_i$ | [cov_filters, dim_attend]
        self.U_f_b = tf.Variable(np.zeros((self.dim_attend,)).astype('float32'), name='U_f_b')  # $U_f x f_i + U_f_b$ | [dim_attend, ]

        self.U_a = tf.Variable(norm_weight(self.channels,
            self.dim_attend), name='U_a')                                                      # $U_a x a_i$ | [annotatin_channels, dim_attend]
        self.U_a_b = tf.Variable(np.zeros((self.dim_attend,)).astype('float32'), name='U_a_b') # $U_a x a_i + U_a_b$ | [dim_attend, ]

        self.W_a = tf.Variable(norm_weight(self.dim_decoder,
            self.dim_attend), name='W_a')                                                      # $W_a x h_{t_1}$ | [dec_dim, dim_attend]
        self.W_a_b = tf.Variable(np.zeros((self.dim_attend,)).astype('float32'), name='W_a_b') # $W_a x h_{t-1} + W_a_b$ | [dim_attend, ]

        self.V_a = tf.Variable(norm_weight(self.dim_attend, 1), name='V_a')                    # $V_a x tanh(A + B + C)$ | [dim_attend, 1]
        self.V_a_b = tf.Variable(np.zeros((1,)).astype('float32'), name='V_a_b')               # $V_a x tanh(A + B + C) + V_a_b$ | [1, ]

        self.alpha_past_filter = tf.Variable(conv_norm_weight(1, self.dim_attend, self.coverage_kernel), name='alpha_past_filter')

    def get_context(self, annotation4ctx, h_t_1, alpha_past4ctx, a_mask):

        #### calculate $U_f x f_i$ ####
        alpha_past_4d = alpha_past4ctx[:, :, :, None]

        Ft = tf.nn.conv2d(alpha_past_4d, filter=self.alpha_past_filter, strides=[1, 1, 1, 1], padding='SAME')

        coverage_vector = tf.tensordot(Ft, self.U_f, axes=1) \
        + self.U_f_b                                            # [batch, h, w, dim_attend]

        #### calculate $U_a x a_i$ ####
        watch_vector = tf.tensordot(annotation4ctx, self.U_a, axes=1) \
        + self.U_a_b                                            # [batch, h, w, dim_attend]

        #### calculate $W_a x h_{t - 1}$ ####
        speller_vector = tf.tensordot(h_t_1, self.W_a, axes=1) \
        + self.W_a_b                                            # [batch, dim_attend]
        speller_vector = speller_vector[:, None, None, :]       # [batch, None, None, dim_attend]

        tanh_vector = tf.tanh(
            coverage_vector + watch_vector + speller_vector)    # [batch, h, w, dim_attend]

        e_ti = tf.tensordot(tanh_vector, self.V_a, axes=1) + self.V_a_b  # [batch, h, w, 1]

        alpha = tf.exp(e_ti)

        alpha = tf.squeeze(alpha, axis=3)

        if a_mask is not None:
            alpha = alpha * a_mask

        alpha = alpha / tf.reduce_sum(alpha, \
            axis=[1, 2], keepdims=True)                         # normlized weights | [batch, h, w]

        alpha_past4ctx += alpha                                 # accumalated weights matrix | [batch, h, w]

        context = tf.reduce_sum(annotation4ctx * alpha[:, :, :, None], \
            axis=[1, 2])                                        # context vector | [batch, feature_channels]

        return context, alpha, alpha_past4ctx


class WAP():
    def __init__(self, watcher, attender, parser, hidden_dim, word_dim, context_dim, target_dim, training):
        #self.batch_size = batch_size
        self.hidden_dim = hidden_dim
        self.word_dim = word_dim
        self.context_dim = context_dim
        self.target_dim = target_dim
        self.embed_matrix = tf.Variable(norm_weight(self.target_dim, self.word_dim), name='embed')

        self.watcher = watcher
        self.attender = attender
        self.parser = parser
        self.Wa2h = tf.Variable(norm_weight(self.context_dim, self.hidden_dim), name='Wa2h')
        self.ba2h = tf.Variable(np.zeros((self.hidden_dim,)).astype('float32'), name='ba2h')
        self.Wc = tf.Variable(norm_weight(self.context_dim, self.word_dim), name='Wc')
        self.bc = tf.Variable(np.zeros((self.word_dim,)).astype('float32'), name='bc')
        self.Wh = tf.Variable(norm_weight(self.hidden_dim, self.word_dim), name='Wh')
        self.bh = tf.Variable(np.zeros((self.word_dim,)).astype('float32'), name='bh')
        self.Wy = tf.Variable(norm_weight(self.word_dim, self.word_dim), name='Wy')
        self.by = tf.Variable(np.zeros((self.word_dim,)).astype('float32'), name='by')
        self.Wo = tf.Variable(norm_weight(self.word_dim//2, self.target_dim), name='Wo')
        self.bo = tf.Variable(np.zeros((self.target_dim,)).astype('float32'), name='bo')
        self.training = training

    def get_word(self, sample_y, sample_h_pre, alpha_past_pre, sample_annotation):

        emb = tf.cond(sample_y[0] < 0,
            lambda: tf.fill((1, self.word_dim), 0.0),
            lambda: tf.nn.embedding_lookup(self.embed_matrix, sample_y)
            )

        #ret = self.parser.one_time_step((h_pre, None, None, alpha_past_pre, annotation, None), (emb, None))
        emb_y_z_r_vector = tf.tensordot(emb, self.parser.W_yz_yr, axes=1) + \
        self.parser.b_yz_yr                                            # [batch, 2 * dim_decoder]
        hidden_z_r_vector = tf.tensordot(sample_h_pre,
        self.parser.U_hz_hr, axes=1)                                   # [batch, 2 * dim_decoder]
        pre_z_r_vector = tf.sigmoid(emb_y_z_r_vector + \
        hidden_z_r_vector)                                             # [batch, 2 * dim_decoder]

        r1 = pre_z_r_vector[:, :self.parser.hidden_dim]                # [batch, dim_decoder]
        z1 = pre_z_r_vector[:, self.parser.hidden_dim:]                # [batch, dim_decoder]

        emb_y_h_vector = tf.tensordot(emb, self.parser.W_yh, axes=1) + \
        self.parser.b_yh                                               # [batch, dim_decoder]
        hidden_r_h_vector = tf.tensordot(sample_h_pre,
        self.parser.U_rh, axes=1)                                      # [batch, dim_decoder]
        hidden_r_h_vector *= r1
        pre_h_proposal = tf.tanh(hidden_r_h_vector + emb_y_h_vector)

        pre_h = z1 * sample_h_pre + (1. - z1) * pre_h_proposal

        context, _, alpha_past = self.parser.attender.get_context(sample_annotation, pre_h, alpha_past_pre, None)  # [batch, dim_ctx]
        emb_y_z_r_nl_vector = tf.tensordot(pre_h, self.parser.U_hz_hr_nl, axes=1) + self.parser.b_hz_hr_nl
        context_z_r_vector = tf.tensordot(context, self.parser.W_c_z_r, axes=1)
        z_r_vector = tf.sigmoid(emb_y_z_r_nl_vector + context_z_r_vector)

        r2 = z_r_vector[:, :self.parser.hidden_dim]
        z2 = z_r_vector[:, self.parser.hidden_dim:]

        emb_y_h_nl_vector = tf.tensordot(pre_h, self.parser.U_rh_nl, axes=1) + self.parser.b_rh_nl
        emb_y_h_nl_vector *= r2
        context_h_vector = tf.tensordot(context, self.parser.W_c_h_nl, axes=1)
        h_proposal = tf.tanh(emb_y_h_nl_vector + context_h_vector)
        h = z2 * pre_h + (1. - z2) * h_proposal

        h_t = h
        c_t = context
        alpha_past_t = alpha_past
        y_t_1 = emb
        logit_gru = tf.tensordot(h_t, self.Wh, axes=1) + self.bh
        logit_ctx = tf.tensordot(c_t, self.Wc, axes=1) + self.bc
        logit_pre = tf.tensordot(y_t_1, self.Wy, axes=1) + self.by
        logit = logit_pre + logit_ctx + logit_gru   # batch x word_dim

        shape = tf.shape(logit)
        logit = tf.reshape(logit, [-1, shape[1]//2, 2])
        logit = tf.reduce_max(logit, axis=2)

        logit = tf.layers.dropout(inputs=logit, rate=0.2, training=self.training)

        logit = tf.tensordot(logit, self.Wo, axes=1) + self.bo

        next_probs = tf.nn.softmax(logits=logit)
        next_word  = tf.reduce_max(tf.multinomial(next_probs, num_samples=1), axis=1)
        return next_probs, next_word, h_t, alpha_past_t

    def get_cost(self, cost_annotation, cost_y, a_m, y_m):
        timesteps = tf.shape(cost_y)[0]
        batch_size = tf.shape(cost_y)[1]
        emb_y = tf.nn.embedding_lookup(self.embed_matrix, tf.reshape(cost_y, [-1]))
        emb_y = tf.reshape(emb_y, [timesteps, batch_size, self.word_dim])
        emb_pad = tf.fill((1, batch_size, self.word_dim), 0.0)
        emb_shift = tf.concat([emb_pad ,tf.strided_slice(emb_y, [0, 0, 0], [-1, batch_size, self.word_dim], [1, 1, 1])], axis=0)
        new_emb_y = emb_shift
        anno_mean = tf.reduce_sum(cost_annotation * a_m[:, :, :, None], axis=[1, 2]) / tf.reduce_sum(a_m, axis=[1, 2])[:, None]
        h_0 = tf.tensordot(anno_mean, self.Wa2h, axes=1) + self.ba2h  # [batch, hidden_dim]
        h_0 = tf.tanh(h_0)

        ret = self.parser.get_ht_ctx(new_emb_y, h_0, cost_annotation, a_m, y_m)
        h_t = ret[0]                      # h_t of all timesteps [timesteps, batch, word_dim]
        c_t = ret[1]                      # c_t of all timesteps [timesteps, batch, context_dim]

        y_t_1 = new_emb_y                 # shifted y | [1:] = [:-1]
        logit_gru = tf.tensordot(h_t, self.Wh, axes=1) + self.bh
        logit_ctx = tf.tensordot(c_t, self.Wc, axes=1) + self.bc
        logit_pre = tf.tensordot(y_t_1, self.Wy, axes=1) + self.by
        logit = logit_pre + logit_ctx + logit_gru
        shape = tf.shape(logit)
        logit = tf.reshape(logit, [shape[0], -1, shape[2]//2, 2])
        logit = tf.reduce_max(logit, axis=3)

        logit = tf.layers.dropout(inputs=logit, rate=0.2, training=self.training)

        logit = tf.tensordot(logit, self.Wo, axes=1) + self.bo
        logit_shape = tf.shape(logit)
        logit = tf.reshape(logit, [-1,
            logit_shape[2]])
        cost = tf.nn.softmax_cross_entropy_with_logits_v2(logits=logit, labels=tf.one_hot(tf.reshape(cost_y, [-1]),
            depth=self.target_dim))

        cost = tf.multiply(cost, tf.reshape(y_m, [-1]))
        cost = tf.reshape(cost, [shape[0], shape[1]])
        cost = tf.reduce_sum(cost, axis=0)
        cost = tf.reduce_mean(cost)
        return cost


    def get_sample(self, anno, infer_y, h_pre, alpha_past, if_trainning, p, w, h, alpha, ctx0, h_0, k , maxlen, stochastic, session, training):

        sample = []
        sample_score = []

        live_k = 1
        dead_k = 0

        hyp_samples = [[]] * live_k
        hyp_scores = np.zeros(live_k).astype('float32')
        hyp_states = []


        next_alpha_past = np.zeros((ctx0.shape[0], ctx0.shape[1], ctx0.shape[2])).astype('float32')
        emb_0 = np.zeros((ctx0.shape[0], 256))

        next_w = -1 * np.ones((1,)).astype('int64')

        next_state = h_0
        for ii in range(maxlen):

            ctx = np.tile(ctx0, [live_k, 1, 1, 1])

            input_dict = {
            anno:ctx,
            infer_y:next_w,
            alpha_past:next_alpha_past,
            h_pre:next_state,
            if_trainning:training
            }

            next_p, next_w, next_state, next_alpha_past = session.run([p, w, h, alpha], feed_dict=input_dict)

            if stochastic:
                if argmax:
                    nw = next_p[0].argmax()
                else:
                    nw = next_w[0]
                sample.append(nw)
                sample_score += next_p[0, nw]
                if nw == 0:
                    break
            else:
                cand_scores = hyp_scores[:, None] - np.log(next_p)
                cand_flat = cand_scores.flatten()
                ranks_flat = cand_flat.argsort()[:(k-dead_k)]
                voc_size = next_p.shape[1]

                assert voc_size==111

                trans_indices = ranks_flat // voc_size
                word_indices = ranks_flat % voc_size
                costs = cand_flat[ranks_flat]
                new_hyp_samples = []
                new_hyp_scores = np.zeros(k-dead_k).astype('float32')
                new_hyp_states = []
                new_hyp_alpha_past = []

                for idx, [ti, wi] in enumerate(zip(trans_indices, word_indices)):
                    new_hyp_samples.append(hyp_samples[ti]+[wi])
                    new_hyp_scores[idx] = copy.copy(costs[idx])
                    new_hyp_states.append(copy.copy(next_state[ti]))
                    new_hyp_alpha_past.append(copy.copy(next_alpha_past[ti]))

                new_live_k = 0
                hyp_samples = []
                hyp_scores = []
                hyp_states = []
                hyp_alpha_past = []

                for idx in range(len(new_hyp_samples)):
                    if new_hyp_samples[idx][-1] == 0: # <eol>
                        sample.append(new_hyp_samples[idx])
                        sample_score.append(new_hyp_scores[idx])
                        dead_k += 1
                    else:
                        new_live_k += 1
                        hyp_samples.append(new_hyp_samples[idx])
                        hyp_scores.append(new_hyp_scores[idx])
                        hyp_states.append(new_hyp_states[idx])
                        hyp_alpha_past.append(new_hyp_alpha_past[idx])
                hyp_scores = np.array(hyp_scores)
                live_k = new_live_k

                if new_live_k < 1:
                    break
                if dead_k >= k:
                    break

                next_w = np.array([w[-1] for w in hyp_samples])
                next_state = np.array(hyp_states)
                next_alpha_past = np.array(hyp_alpha_past)

        if not stochastic:
            # dump every remaining one
            if live_k > 0:
                for idx in range(live_k):
                    sample.append(hyp_samples[idx])
                    sample_score.append(hyp_scores[idx])

        return sample, sample_score

class Parser():

    def __init__(self, hidden_dim, word_dim, attender, context_dim):

        self.attender = attender                                # inner-instance of Attender to provide context
        self.context_dim = context_dim                          # context dime 684
        self.hidden_dim = hidden_dim                            # dim of hidden state  256
        self.word_dim = word_dim                                # dim of embedding word 256

        self.W_yz_yr = tf.Variable(np.concatenate(
            [norm_weight(self.word_dim, self.hidden_dim), norm_weight(self.word_dim, self.hidden_dim)], axis=1), name='W_yz_yr') # [dim_word, 2 * dim_decoder]
        self.b_yz_yr = tf.Variable(np.zeros((2 * self.hidden_dim, )).astype('float32'), name='b_yz_yr')

        self.U_hz_hr = tf.Variable(np.concatenate(
            [ortho_weight(self.hidden_dim),ortho_weight(self.hidden_dim)], axis=1), name='U_hz_hr')                              # [dim_hidden, 2 * dim_hidden]

        self.W_yh = tf.Variable(norm_weight(self.word_dim,
            self.hidden_dim), name='W_yh')
        self.b_yh = tf.Variable(np.zeros((self.hidden_dim, )).astype('float32'), name='b_yh')                                    # [dim_decoder, ]

        self.U_rh = tf.Variable(ortho_weight(self.hidden_dim), name='U_rh')                                                      # [dim_hidden, dim_hidden]

        self.U_hz_hr_nl = tf.Variable(np.concatenate(
            [ortho_weight(self.hidden_dim), ortho_weight(self.hidden_dim)], axis=1), name='U_hz_hr_nl')                          # [dim_hidden, 2 * dim_hidden] non_linear

        self.b_hz_hr_nl = tf.Variable(np.zeros((2 * self.hidden_dim, )).astype('float32'), name='b_hz_hr_nl')                    # [2 * dim_hidden, ]

        self.W_c_z_r = tf.Variable(norm_weight(self.context_dim,
            2 * self.hidden_dim), name='W_c_z_r')

        self.U_rh_nl = tf.Variable(ortho_weight(self.hidden_dim), name='U_rh_nl')
        self.b_rh_nl = tf.Variable(np.zeros((self.hidden_dim, )).astype('float32'), name='b_rh_nl')

        self.W_c_h_nl = tf.Variable(norm_weight(self.context_dim, self.hidden_dim), name='W_c_h_nl')

    def get_ht_ctx(self, emb_y, target_hidden_state_0, annotations, a_m, y_m):

        res = tf.scan(self.one_time_step, elems=(emb_y, y_m),
            initializer=(target_hidden_state_0,
                tf.zeros([tf.shape(annotations)[0], self.context_dim]),
                tf.zeros([tf.shape(annotations)[0], tf.shape(annotations)[1], tf.shape(annotations)[2]]),
                tf.zeros([tf.shape(annotations)[0], tf.shape(annotations)[1], tf.shape(annotations)[2]]),
                annotations, a_m))

        return res
