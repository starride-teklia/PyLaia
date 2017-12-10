import re
import torch.nn as nn
import torch.nn.functional as F

from .conv_block import ConvBlock
from torch.nn.utils.rnn import PackedSequence, pack_padded_sequence
from laia.data import PaddedTensor

class CnnRnn(nn.Module):
    def __init__(self, num_input_channels, num_output_labels,
                 cnn_num_features,
                 cnn_kernel_size,
                 cnn_dilation,
                 cnn_activation,
                 cnn_poolsize,
                 cnn_dropout,
                 cnn_batchnorm,
                 rnn_units,
                 rnn_layers,
                 rnn_dropout,
                 lin_dropout,
                 collapse='mean'):
        super(CnnRnn, self).__init__()
        self._collapse = collapse
        self._rnn_dropout = rnn_dropout
        self._lin_dropout = lin_dropout
        # Add convolutional blocks, in a VGG style.
        self._conv_blocks = []
        ni = num_input_channels
        for i, (nh, ks, di, f, ps, dr, bn) in enumerate(
                zip(cnn_num_features, cnn_kernel_size, cnn_dilation,
                    cnn_activation, cnn_poolsize, cnn_dropout, cnn_batchnorm)):
            layer = ConvBlock(ni, nh, kernel_size=ks, dilation=di,
                              activation=f, poolsize=ps, dropout=dr,
                              batchnorm=bn)
            ni = nh
            self.add_module('conv_block%d' % i, layer)
            self._conv_blocks.append(layer)

        if collapse.startswith('adaptive_max'):
            self._adaptive_rows = int(re.match('adaptive_max-([0-9]+)', collapse).group(1))
            ni = ni * self._adaptive_rows

        rnn = nn.LSTM(ni, rnn_units, rnn_layers, dropout=rnn_dropout,
                      bidirectional=True, batch_first=False)
        self.add_module('rnn', rnn)
        self._rnn = rnn

        linear = nn.Linear(2 * rnn_units, num_output_labels)
        self.add_module('linear', linear)
        self._linear = linear

    def forward(self, x):
        is_padded = isinstance(x, PaddedTensor)
        for block in self._conv_blocks:
            x = block(x)
        if is_padded:
            x, xs = x.data, x.sizes
        # Note: x shape is N x C x H x W
        if self._collapse == 'sum':
            x = x.sum(dim=2, keepdim=True)
        elif self._collapse == 'mean':
            x = x.mean(dim=2, keepdim=True)
        elif self._collapse == 'max':
            x, _ = x.max(dim=2, keepdim=True)
        elif self._collapse.startswith('adaptive_max'):
            x = F.adaptive_max_pool2d(x, (self._adaptive_rows, x.size(3)), False)
        else:
            raise NotImplementedError(
                'Collapse method "%s" is not implemented' % self._collapse)
        # Note: x shape is typically N x C x 1 x W
        N, C, H, W = tuple(x.size())
        x = x.permute(3, 0, 1, 2).contiguous().view(W, N, C * H)

        if self._rnn_dropout > 0.0:
            x = F.dropout(x, self._rnn_dropout, training=self.training)
        if is_padded:
            x = pack_padded_sequence(x, list(xs[:,1]))
        x, _ = self._rnn(x)
        # Output linear layer
        if is_padded:
            x, xs = x.data, x.batch_sizes
        if self._lin_dropout > 0.0:
            x = F.dropout(x, self._lin_dropout, training=self.training)
        x = self._linear(x)
        if is_padded:
            x = PackedSequence(data=x, batch_sizes=xs)
        return x