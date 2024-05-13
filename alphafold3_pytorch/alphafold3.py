from __future__ import annotations
from functools import partial

import torch
from torch import nn
from torch import Tensor
import torch.nn.functional as F

from torch.nn import (
    Module,
    ModuleList,
    Linear,
    Sequential,
)

from typing import Literal, Tuple

from alphafold3_pytorch.typing import (
    Float,
    Int,
    Bool,
    typecheck
)

from alphafold3_pytorch.attention import Attention

from einops import rearrange, repeat, einsum, pack, unpack
from einops.layers.torch import Rearrange

# constants

LinearNoBias = partial(Linear, bias = False)

# helper functions

def exists(v):
    return v is not None

def default(v, d):
    return v if exists(v) else d

def pack_one(t, pattern):
    return pack([t], pattern)

def unpack_one(t, ps, pattern):
    return unpack(t, ps, pattern)[0]

# classic feedforward, SwiGLU variant
# they name this 'transition' in their paper
# Algorithm 11

class SwiGLU(Module):
    @typecheck
    def forward(
        self,
        x: Float['b n d']
    ) -> Float['b n (d//2)']:

        x, gates = x.chunk(2, dim = -1)
        return F.silu(gates) * x

class Transition(Module):
    def __init__(
        self,
        *,
        dim,
        expansion_factor = 4
    ):
        super().__init__()
        dim_inner = int(dim * expansion_factor)

        self.ff = Sequential(
            LinearNoBias(dim, dim_inner * 2),
            SwiGLU(),
            LinearNoBias(dim_inner, dim)
        )

    @typecheck
    def forward(
        self,
        x: Float['b n d']
    ) -> Float['b n d']:

        return self.ff(x)

# normalization
# both pre layernorm as well as adaptive layernorm wrappers

class PreLayerNorm(Module):
    @typecheck
    def __init__(
        self,
        fn: Attention | Transition | TriangleAttention | TriangleMultiplication | AttentionPairBias,
        *,
        dim,
    ):
        super().__init__()
        self.fn = fn
        self.norm = nn.LayerNorm(dim)

    @typecheck
    def forward(
        self,
        x: Float['... n d'],
        **kwargs
    ) -> Float['... n d']:

        x = self.norm(x)
        return self.fn(x, **kwargs)

class AdaptiveLayerNorm(Module):
    """ Algorithm 26 """

    def __init__(
        self,
        *,
        dim,
        dim_cond
    ):
        super().__init__()
        self.norm = nn.LayerNorm(dim, elementwise_affine = False)
        self.norm_cond = nn.LayerNorm(dim_cond, bias = False)

        self.to_gamma = nn.Sequential(
            Linear(dim_cond, dim),
            nn.Sigmoid()
        )

        self.to_beta = LinearNoBias(dim_cond, dim)

    @typecheck
    def forward(
        self,
        x: Float['b ... n d'],
        cond: Float['b ... dc']
    ) -> Float['b ... n d']:

        normed = self.norm(x)
        normed_cond = self.norm_cond(cond)

        gamma = self.to_gamma(normed_cond)
        beta = self.to_beta(normed_cond)
        return normed * gamma + beta

class ConditionWrapper(Module):
    """ Algorithm 25 """

    @typecheck
    def __init__(
        self,
        fn: Attention | Transition | TriangleAttention |  AttentionPairBias,
        *,
        dim,
        dim_cond,
        adaln_zero_bias_init_value = -2.
    ):
        super().__init__()
        self.fn = fn
        self.adaptive_norm = AdaptiveLayerNorm(dim = dim, dim_cond = dim_cond)

        adaln_zero_gamma_linear = Linear(dim_cond, dim)
        nn.init.zeros_(adaln_zero_gamma_linear.weight)
        nn.init.constant_(adaln_zero_gamma_linear.bias, adaln_zero_bias_init_value)

        self.to_adaln_zero_gamma = nn.Sequential(
            adaln_zero_gamma_linear,
            nn.Sigmoid()
        )

    @typecheck
    def forward(
        self,
        x: Float['b ... n d'],
        *,
        cond: Float['b ... dc'],
        **kwargs
    ) -> Float['b ... n d']:
        x = self.adaptive_norm(x, cond = cond)

        out = self.fn(x, **kwargs)

        gamma = self.to_adaln_zero_gamma(cond)
        return out * gamma

# triangle multiplicative module
# seems to be unchanged from alphafold2

class TriangleMultiplication(Module):

    @typecheck
    def __init__(
        self,
        *,
        dim,
        dim_hidden = None,
        mix: Literal["incoming", "outgoing"] = 'incoming'
    ):
        super().__init__()

        dim_hidden = default(dim_hidden, dim)
        self.norm = nn.LayerNorm(dim)

        self.left_proj = Linear(dim, dim_hidden)
        self.right_proj = Linear(dim, dim_hidden)

        self.left_gate = Linear(dim, dim_hidden)
        self.right_gate = Linear(dim, dim_hidden)
        self.out_gate = Linear(dim, dim_hidden)

        # initialize all gating to be identity

        for gate in (self.left_gate, self.right_gate, self.out_gate):
            nn.init.constant_(gate.weight, 0.)
            nn.init.constant_(gate.bias, 1.)

        if mix == 'outgoing':
            self.mix_einsum_eq = '... i k d, ... j k d -> ... i j d'
        elif mix == 'incoming':
            self.mix_einsum_eq = '... k j d, ... k i d -> ... i j d'

        self.to_out_norm = nn.LayerNorm(dim_hidden)
        self.to_out = Linear(dim_hidden, dim)

    @typecheck
    def forward(
        self,
        x: Float['b n n d'],
        mask: Bool['b n'] | None = None
    ) -> Float['b n n d']:

        if exists(mask):
            mask = rearrange(mask, 'b i -> b i 1 1') & rearrange(mask, 'b j -> b 1 j 1')

        x = self.norm(x)

        left = self.left_proj(x)
        right = self.right_proj(x)

        if exists(mask):
            left = left * mask
            right = right * mask

        left_gate = self.left_gate(x).sigmoid()
        right_gate = self.right_gate(x).sigmoid()
        out_gate = self.out_gate(x).sigmoid()

        left = left * left_gate
        right = right * right_gate

        out = einsum(left, right, self.mix_einsum_eq)

        out = self.to_out_norm(out)
        out = out * out_gate
        return self.to_out(out)

# there are two types of attention in this paper, triangle and attention-pair-bias
# they differ by how the attention bias is computed
# triangle is axial attention w/ itself projected for bias

class AttentionPairBias(Module):
    def __init__(
        self,
        *,
        heads,
        dim_pairwise_repr,
        max_seq_len = 16384,
        **attn_kwargs
    ):
        super().__init__()
        self.attn = Attention(heads = heads, **attn_kwargs)

        # line 8 of Algorithm 24

        to_attn_bias_linear = LinearNoBias(dim_pairwise_repr, heads)
        nn.init.zeros_(to_attn_bias_linear.weight)

        self.to_attn_bias = nn.Sequential(
            nn.LayerNorm(dim_pairwise_repr),
            to_attn_bias_linear,
            Rearrange('... i j h -> ... h i j')
        )

        self.max_seq_len = max_seq_len
        self.attn_bias_bias = nn.Parameter(torch.zeros(max_seq_len, max_seq_len))

    @typecheck
    def forward(
        self,
        single_repr: Float['b n ds'],
        *,
        pairwise_repr: Float['b n n dp'],
        **kwargs
    ) -> Float['b n ds']:

        seq = single_repr.shape[1]
        assert seq <= self.max_seq_len

        attn_bias = self.to_attn_bias(pairwise_repr) + self.attn_bias_bias[:seq, :seq]

        out = self.attn(
            single_repr,
            attn_bias = attn_bias,
            **kwargs
        )

        return out

class TriangleAttention(Module):
    def __init__(
        self,
        *,
        dim,
        heads,
        node_type: Literal['starting', 'ending'],
        **attn_kwargs
    ):
        super().__init__()
        self.need_transpose = node_type == 'ending'

        self.attn = Attention(dim = dim, heads = heads, **attn_kwargs)

        self.to_attn_bias = nn.Sequential(
            LinearNoBias(dim, heads),
            Rearrange('... i j h -> ... h i j')
        )

    @typecheck
    def forward(
        self,
        pairwise_repr: Float['b n n d'],
        mask: Bool['b n'] | None = None,
        **kwargs
    ) -> Float['b n n d']:

        if self.need_transpose:
            pairwise_repr = rearrange(pairwise_repr, 'b i j d -> b j i d')

        attn_bias = self.to_attn_bias(pairwise_repr)

        batch_repeat = pairwise_repr.shape[1]
        attn_bias = repeat(attn_bias, 'b ... -> (b r) ...', r = batch_repeat)

        if exists(mask):
            mask = repeat(mask, 'b ... -> (b r) ...', r = batch_repeat)

        pairwise_repr, packed_shape = pack_one(pairwise_repr, '* n d')

        out = self.attn(
            pairwise_repr,
            mask = mask,
            attn_bias = attn_bias,
            **kwargs
        )

        out = unpack_one(out, packed_shape, '* n d')

        if self.need_transpose:
            out = rearrange(out, 'b j i d -> b i j d')

        return out

# pairformer stack

class PairformerStack(Module):
    def __init__(
        self,
        *,
        dim_single,
        dim_pairwise,
        depth = 48,
        tri_mult_dim_hidden = None,
        tri_attn_dim_head = 32,
        tri_attn_heads = 4,
        pair_bias_attn_dim_head = 64,
        pair_bias_attn_heads = 16,
        dropout_row_prob = 0.25,
        dropout_col_prob = 0.25
    ):
        super().__init__()
        layers = ModuleList([])

        tri_mult_kwargs = dict(
            dim = dim_pairwise,
            dim_hidden = tri_mult_dim_hidden
        )

        tri_attn_kwargs = dict(
            dim = dim_pairwise,
            heads = tri_attn_heads,
            dim_head = tri_attn_dim_head
        )

        pair_bias_attn_kwargs = dict(
            dim = dim_single,
            dim_pairwise_repr = dim_pairwise,
            heads = pair_bias_attn_heads,
            dim_head = pair_bias_attn_dim_head
        )

        for _ in range(depth):
            dropout_row = nn.Dropout(dropout_row_prob)
            dropout_col = nn.Dropout(dropout_col_prob)

            pairwise_pre_ln = partial(PreLayerNorm, dim = dim_pairwise)

            tri_mult_outgoing = TriangleMultiplication(mix = 'outgoing', **tri_mult_kwargs)
            tri_mult_incoming = TriangleMultiplication(mix = 'incoming', **tri_mult_kwargs)
            tri_attn_starting = TriangleAttention(node_type = 'starting', **tri_attn_kwargs)
            tri_attn_ending = TriangleAttention(node_type = 'ending', **tri_attn_kwargs)

            pairwise_transition = Transition(dim = dim_pairwise)

            single_pre_ln = partial(PreLayerNorm, dim = dim_single)

            pair_bias_attn = AttentionPairBias(**pair_bias_attn_kwargs)
            single_transition = Transition(dim = dim_single)

            layers.append(ModuleList([
                dropout_row,
                dropout_col,
                pairwise_pre_ln(tri_mult_outgoing),
                pairwise_pre_ln(tri_mult_incoming),
                pairwise_pre_ln(tri_attn_starting),
                pairwise_pre_ln(tri_attn_ending),
                pairwise_pre_ln(pairwise_transition),
                single_pre_ln(pair_bias_attn),
                single_pre_ln(single_transition),
            ]))

        self.layers = layers

    @typecheck
    def forward(
        self,
        *,
        single_repr: Float['b n ds'],
        pairwise_repr: Float['b n n dp'],
        mask: Bool['b n'] | None = None

    ) -> Tuple[Float['b n ds'], Float['b n n dp']]:

        for dropout_row, dropout_col, tri_mult_outgoing, tri_mult_incoming, tri_attn_starting, tri_attn_ending, pairwise_transition, pair_bias_attn, single_transition in self.layers:

            pairwise_repr = dropout_row(tri_mult_outgoing(pairwise_repr, mask = mask)) + pairwise_repr
            pairwise_repr = dropout_row(tri_mult_incoming(pairwise_repr, mask = mask)) + pairwise_repr
            pairwise_repr = dropout_row(tri_attn_starting(pairwise_repr, mask = mask)) + pairwise_repr
            pairwise_repr = dropout_col(tri_attn_ending(pairwise_repr, mask = mask)) + pairwise_repr

            pairwise_repr, packed_shape = pack_one(pairwise_repr, 'b * d')
            pairwise_repr = pairwise_transition(pairwise_repr) + pairwise_repr
            pairwise_repr = unpack_one(pairwise_repr, packed_shape, 'b * d')

            single_repr = pair_bias_attn(single_repr, pairwise_repr = pairwise_repr, mask = mask) + single_repr
            single_repr = single_transition(single_repr) + single_repr

        return single_repr, pairwise_repr

# main class

class Alphafold3(Module):
    def __init__(self):
        super().__init__()
