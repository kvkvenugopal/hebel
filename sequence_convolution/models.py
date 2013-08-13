import numpy as np
from pycuda import gpuarray
from pycuda.curandom import rand as curand
from pycuda.driver import Stream
from itertools import izip
from scikits.cuda import linalg
from . import pycuda_ops
from neural_nets.models import HiddenLayer, NeuralNet
from neural_nets.pycuda_ops.elementwise import sign, sample_dropout_mask, \
     apply_dropout_mask
from neural_nets.pycuda_ops.matrix import extract_columns, insert_columns


class SequenceConvolutionLayer(HiddenLayer):
    n_parameters = 2

    def __init__(self, n_in, filter_width, n_filters, activation_function='sigmoid',
                 weights_scale=.01, W=None, b=None,
                 l1_penalty_weight=0., l2_penalty_weight=0.,
                 dtype=np.float32):
        if W is None:
            self.W = weights_scale * \
              curand((n_filters, 4*filter_width), dtype=dtype) \
              -.5 * weights_scale
        else:
            self.W = W

        if b is None:
            self.b = gpuarray.zeros((n_filters,), dtype)
        else:
            self.b = b

        assert self.W.shape == (n_filters, 4*filter_width)
        assert self.b.shape == (n_filters,)

        self.n_in = n_in
        self.filter_width = filter_width
        self.n_filters = n_filters
        self.n_units = n_filters * n_in

        self._set_activation_fct(activation_function)
        self.l1_penalty_weight = l1_penalty_weight
        self.l2_penalty_weight = l2_penalty_weight

        self.lr_multiplier = [1., 1.]

    def feed_forward(self, input, prediction=False):
        activations = \
            pycuda_ops.convolve_sequence(input, self.W, self.b)

        self.f(activations)
        return (activations,)

    def backprop(self, input, df_output, cache=None):
        if cache is None:
            activations = self.feed_forward(input)[0]
        else:
            activations = cache[0]

        df_activations = self.df(activations)
        delta = df_activations * df_output
        df_b = pycuda_ops.sum_delta(delta, self.n_filters)
        df_W = pycuda_ops.convolve_sequence_gradient(
            input, delta,
            self.filter_width, self.n_filters)

        # L1 weight decay
        if self.l1_penalty_weight:
            df_W -= self.l1_penalty_weight * sign(self.W)

        # L2 weight decay
        if self.l2_penalty_weight:
            df_W -= self.l2_penalty_weight * self.W

        return (df_W, df_b), None

class MaxPoolingLayer(HiddenLayer):
    n_parameters = 0
    lr_multiplier = []

    def __init__(self, n_in, pool_size, n_filters, dropout=False,
                 l1_penalty_weight=0., l2_penalty_weight=0.):
        self.n_in = n_in
        self.pool_size = pool_size
        self.n_filters = n_filters

        self.l1_penalty_weight = 0.
        self.l2_penalty_weight = 0.

        self.dropout = dropout

        self.n_units = self._compute_n_units(n_in, pool_size, n_filters)

    @staticmethod
    def _compute_n_units(n_in, pool_size, n_filters):
        """ Compute the number of output units """
        return int(np.ceil(n_in / float(pool_size))) * n_filters

    @property
    def parameters(self):
        return []

    @parameters.setter
    def parameters(self, value):
        pass

    def update_parameters(self, values, stream=None):
        pass

    @property
    def l1_penalty(self):
        return 0.

    @property
    def l2_penalty(self):
        return 0.

    def feed_forward(self, input, prediction=False):
        activations, argmax = pycuda_ops.max_pool(input, self.pool_size, self.n_filters)

        if self.dropout and prediction:
            activations *= .5

        if self.dropout and not prediction:
            dropout_mask = sample_dropout_mask(activations)
            return activations, argmax, dropout_mask

        return activations, argmax

    def backprop(self, input, df_output, cache=None):
        if cache is None:
            cache = self.feed_forward(input)

        if len(cache) == 2:
            activations, argmax = cache
        elif len(cache) == 3:
            activations, argmax, dropout_mask = cache
        else:
            raise ValueError

        if self.dropout and dropout_mask is not None:
            apply_dropout_mask(df_output, dropout_mask)

        n, fm = activations.shape
        activations = activations.reshape((n, self.n_filters, 
                                           fm / self.n_filters))
        df_input = pycuda_ops.max_pool_gradient(input, argmax,
                                                df_output,
                                                self.pool_size,
                                                self.n_filters)
        return tuple(), df_input

class MultiSequenceConvolutionLayer(HiddenLayer):
    def __init__(self, subregion_layers,
                 fully_connected_layer=None,
                 n_filters=None,
                 filter_width=None,
                 pool_size=None,
                 activation_function=None,
                 dropout=None,
                 lr_multiplier=None,
                 l1_penalty_weight=None,
                 l2_penalty_weight=None,
                 dtype=np.float32,
                 weight_scale=.01):
        self.subregion_layers = subregion_layers
        self.dtype = dtype
        self.dropout = dropout

        self.W = []
        self.b = []

        output_offset = 0
        param_idx = 0
        for layer in subregion_layers:
            n_in = layer['n_in']

            # Replace defaults
            if n_filters is not None:
                layer['n_filters'] = n_filters
            if filter_width is not None:
                layer['filter_width'] = filter_width
            if pool_size is not None:
                layer['pool_size'] = pool_size
            if activation_function is not None:
                layer['activation_function'] = activation_function
            if l1_penalty_weight is not None:
                layer['l1_penalty_weight'] = l1_penalty_weight
            if l2_penalty_weight is not None:
                layer['l2_penalty_weight'] = l2_penalty_weight
            if lr_multiplier is not None:
                layer['lr_multiplier'] = lr_multiplier

            if not layer.has_key('weight_share'):
                layer['layer_type'] = 'master'
                _weight_scale = layer.get('weight_scale', weight_scale)
                if not layer.has_key('W'):
                    W = _weight_scale * \
                      curand((layer['n_filters'], 4*layer['filter_width']),
                             dtype) - .5 * _weight_scale
                else:
                    W = layer['W']

                assert W.shape == (layer['n_filters'], 4*layer['filter_width'])
                self.W.append(W)

                if not layer.has_key('b'):
                    b = gpuarray.zeros((layer['n_filters'],), dtype)
                else:
                    b = layer['b']

                assert b.shape == (layer['n_filters'],)
                self.b.append(b)

                layer['param_idx'] = param_idx
                param_idx += 1

                layer['f'], layer['df'] = \
                  self._resolve_activation_fct(layer['activation_function'])

                if not layer.has_key('l1_penalty_weight'):
                    layer['l1_penalty_weight'] = 0.
                if not layer.has_key('l2_penalty_weight'):
                    layer['l2_penalty_weight'] = 0.
                if not layer.has_key('lr_multiplier'):
                    layer['lr_multiplier'] = 1.

            else:
                layer['layer_type'] = 'slave' 
                master_layer = subregion_layers[layer['weight_share']]                
                layer['n_filters'] = master_layer['n_filters']
                layer['filter_width'] = master_layer['filter_width']
                layer['param_idx'] = master_layer['param_idx']
                layer['activation_function'] = master_layer['activation_function']
                layer['f'] = master_layer['f']
                layer['df'] = master_layer['df']

            layer['n_units'] = MaxPoolingLayer._compute_n_units(
                layer['n_in'], layer['pool_size'], layer['n_filters'])

            layer['output_offset'] = output_offset
            output_offset += layer['n_units']

        if isinstance(fully_connected_layer, dict):
            self.fully_connected_layer = HiddenLayer(*fully_connected_layer)
        elif isinstance(fully_connected_layer, HiddenLayer):
            self.fully_connected_layer = fully_connected_layer
        elif fully_connected_layer is None:
            self.fully_connected_layer = None
        else:
            raise TypeError("fully_connected_layer must be a dictionary or "
              "an instance of HiddenLayer")

        if self.fully_connected_layer is not None and \
          self.fully_connected_layer.dropout:
            raise ValueError("Dropout on fully connected layer is not allowed,"
                             "set dropout on MultiSequenceConvolutionLayer "
                             "instead.")

        self.n_units = sum((layer['n_units'] for layer in subregion_layers))
        if self.fully_connected_layer is not None:
            self.fc_layer_offset = self.n_units
            self.n_units += self.fully_connected_layer.n_units
        else:
            self.fc_layer_offset = 0

        self.master_layers = filter(lambda l: l['layer_type'] == 'master',
                                    self.subregion_layers)

        self.l1_penalty_weight = any((l['l1_penalty_weight'] > 0.
                                      for l in self.master_layers))
        self.l2_penalty_weight = any((l['l2_penalty_weight'] > 0.
                                      for l in self.master_layers))

    @property
    def n_parameters(self):
        n_param = len(self.W) + len(self.b)
        if self.fully_connected_layer is not None:
            n_param += self.fully_connected_layer.n_parameters
        return n_param

    @property
    def n_in(self):
        return sum((l['n_in'] for l in self.subregion_layers))

    @property
    def lr_multiplier(self):
        return 2 * [l.get('lr_multiplier', 1.) for l in self.subregion_layers
                if l['layer_type'] == 'master']

    @property
    def parameters(self):
        param = self.W + self.b
        if self.fully_connected_layer is not None:
            param += self.fully_connected_layer.parameters
        return param

    @parameters.setter
    def parameters(self, value):
        assert len(value) == self.n_parameters

        if self.fully_connected_layer is None:
            conv_params = value
        else:
            n_param = self.n_parameters
            n_param_fc = self.fully_connected_layer.n_parameters
            conv_params = value[:n_param-n_param_fc]
            fc_params = value[n_param-n_param_fc:]
            self.fully_connected_layer.parameters = fc_params

        self.W = conv_params[:len(self.W)]
        self.b = conv_params[len(self.W):]

    def update_parameters(self, values, stream=None):
        assert len(values) == self.n_parameters

        if self.fully_connected_layer is None:
            conv_params = values
        else:
            conv_params = values[:-2]
            fc_params = values[-2:]
            self.fully_connected_layer.update_parameters(fc_params)            

        for (param, (gparam, mult)) \
          in izip(self.W + self.b, conv_params):
          param._axpbyz(1., gparam, mult, param, stream=stream)

    @property
    def l1_penalty(self):
        l1_pen = np.sum(
            [float(l['l1_penalty_weight']) * gpuarray.sum(abs(W)).get()
             for l, W in izip(self.master_layers, self.W)])

        if self.fully_connected_layer is not None:
            l1_pen += self.fully_connected_layer.l1_penalty

        return l1_pen

    @property
    def l2_penalty(self):
        l2_pen = np.sum(
            [float(l['l2_penalty_weight']) * .5 * gpuarray.sum(W ** 2.).get()
             for l, W in izip(self.master_layers, self.W)])

        if self.fully_connected_layer is not None:
            l2_pen += self.fully_connected_layer.l2_penalty

        return l2_pen

    def feed_forward(self, input, prediction=False):
        assert all((input[0].shape[0] == i.shape[0] for i in input[1:]))

        N = input[0].shape[0]
        activations_pooled = gpuarray.empty((N, self.n_units),
                                            self.dtype)
        argmax = gpuarray.empty(activations_pooled.shape,
                                np.uint32)

        filtermaps = []

        for input_region, layer \
            in izip(input, self.subregion_layers):
            W = self.W[layer['param_idx']]
            b = self.b[layer['param_idx']]
            act_fct = layer['f']

            filtermap = pycuda_ops.convolve_sequence(input_region, W, b)
            act_fct(filtermap)
            filtermaps.append(filtermap)
            pycuda_ops.max_pool(filtermap, layer['pool_size'],
                                layer['n_filters'],
                                width=layer['n_in'],
                                pooled_offset=layer['output_offset'],
                                target=activations_pooled, argmax=argmax)

        if self.fully_connected_layer is not None:
            assert len(input) == len(self.subregion_layers) + 1
            activations_fc = \
              self.fully_connected_layer.feed_forward(input[-1],
                                                      prediction)[0]
            insert_columns(activations_fc, activations_pooled,
                           self.fc_layer_offset)
        else:
            activations_fc = None

        if self.dropout and not prediction:
            dropout_mask = sample_dropout_mask(activations_pooled)
        else:
            dropout_mask = None

        return activations_pooled, argmax, filtermaps, dropout_mask, activations_fc

    def backprop(self, input, df_output, cache=None):
        if cache is None:
            cache = self.feed_forward(input)

        activations_pooled, argmax, filtermaps, dropout_mask, fc_cache = cache            

        if self.dropout and dropout_mask is not None:
            apply_dropout_mask(df_output, dropout_mask)

        df_W = []
        df_b = []
        df_filtermaps = []

        for input_region, filtermap, layer \
          in izip(input, filtermaps, self.subregion_layers):
            act_df = layer['df']

            df_filtermap = pycuda_ops.max_pool_gradient(
                filtermap, argmax, df_output, layer['pool_size'],
                layer['n_filters'],
                width_pooled=layer['n_units'] / layer['n_filters'],
                pooled_offset=layer['output_offset'])

            df_filtermaps.append(df_filtermap)

            df_conv = act_df(filtermap)
            delta = df_conv * df_filtermap
            df_b_layer = pycuda_ops.sum_delta(delta, layer['n_filters'])
            df_W_layer = pycuda_ops.convolve_sequence_gradient(
                input_region, delta, layer['filter_width'], layer['n_filters'])

            if layer['layer_type'] == 'master':
                df_W.append(df_W_layer)
                df_b.append(df_b_layer)
            else:
                df_W[layer['param_idx']] += df_W_layer
                df_b[layer['param_idx']] += df_b_layer

        for df_W_i, layer in izip(df_W, self.master_layers):
            if layer['l1_penalty_weight']:
                df_W_i -= layer['l1_penalty_weight'] * sign(df_W_i)
            if layer['l2_penalty_weight']:
                df_W_i -= layer['l2_penalty_weight'] * self.W

        if self.fully_connected_layer is not None:
            assert len(input) == len(self.subregion_layers) + 1
            input_fc = input[-1]
            df_output_fc = extract_columns(df_output,
                                           self.fc_layer_offset,
                                           df_output.shape[1])
            grad_fc, df_input_fc = \
              self.fully_connected_layer.backprop(input_fc, df_output_fc,
                                                  fc_cache)
            return df_W + df_b + list(grad_fc), \
              (df_filtermaps, df_input_fc)

        return df_W + df_b, df_filtermaps

class SequenceConvolutionNet(NeuralNet):
    def __init__(self, n_in, n_out, filter_width, n_filters, 
                 pool_size, layers, activation_function='sigmoid',
                 dropout=False, l1_penalty_weight=0., l2_penalty_weight=0.,
                 **kwargs):

        if np.isscalar(l1_penalty_weight):
            l1_conv = l1_penalty_weight
            l1_nn = l1_penalty_weight
        else:
            l1_conv = l1_penalty_weight[0]
            l1_nn = l1_penalty_weight[1:]

        if np.isscalar(l2_penalty_weight):
            l2_conv = l2_penalty_weight
            l2_nn = l2_penalty_weight
        else:
            l2_conv = l2_penalty_weight[0]
            l2_nn = l2_penalty_weight[1:]

        n_in_nn = n_filters * n_in / pool_size
        conv_layer = SequenceConvolutionLayer(n_in, filter_width, n_filters, 
                                              activation_function=activation_function,
                                              l1_penalty_weight=l1_conv, 
                                              l2_penalty_weight=l2_conv)
        
        max_pool_layer = MaxPoolingLayer(conv_layer.n_units / n_filters, 
                                         pool_size, n_filters, 
                                         dropout=dropout)
        
        hidden_layers = [conv_layer, max_pool_layer] + layers
        
        super(SequenceConvolutionNet, self)\
          .__init__(layers=hidden_layers, 
                    activation_function=activation_function,
                    dropout=dropout, 
                    l1_penalty_weight=l1_nn, 
                    l2_penalty_weight=l2_nn, 
                    n_in=n_in, n_out=n_out, **kwargs)

        self.n_layers = len(layers) + 2
        self.n_in = n_in
        self.n_in_nn = n_in_nn
        self.filter_width = filter_width
        self.n_filters = n_filters
        self.pool_size = pool_size
        
        # self.fully_connected_layers = self.hidden_layers
        # self.hidden_layers = [self.conv_layer, self.max_pool_layer] + self.fully_connected_layers
        
