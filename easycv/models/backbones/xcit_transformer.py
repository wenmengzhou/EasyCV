# Copyright (c) 2015-present, Facebook, Inc.
# All rights reserved.
"""
Implementation of Cross-Covariance Image Transformer (XCiT)
Based on timm and DeiT code bases
https://github.com/rwightman/pytorch-image-models/tree/master/timm
https://github.com/facebookresearch/deit/

XCiT Transformer.
Part of the code is borrowed from:
https://github.com/facebookresearch/xcit/blob/master/xcit.py

"""
import math
from functools import partial

import torch
import torch.nn as nn
from timm.models.layers import DropPath, to_2tuple, trunc_normal_
from timm.models.vision_transformer import Mlp, _cfg

from easycv.utils.checkpoint import load_checkpoint
from easycv.utils.logger import get_root_logger
from ..registry import BACKBONES


class PositionalEncodingFourier(nn.Module):
    """
    Positional encoding relying on a fourier kernel matching the one used in the
    "Attention is all of Need" paper. The implementation builds on DeTR code
    https://github.com/facebookresearch/detr/blob/master/models/position_encoding.py
    """

    def __init__(self, hidden_dim=32, dim=768, temperature=10000):
        super().__init__()
        self.token_projection = nn.Conv2d(hidden_dim * 2, dim, kernel_size=1)
        self.scale = 2 * math.pi
        self.temperature = temperature
        self.hidden_dim = hidden_dim
        self.dim = dim

    def forward(self, B, H, W):
        mask = torch.zeros(B, H,
                           W).bool().to(self.token_projection.weight.device)
        not_mask = ~mask
        y_embed = not_mask.cumsum(1, dtype=torch.float32)
        x_embed = not_mask.cumsum(2, dtype=torch.float32)
        eps = 1e-6
        y_embed = y_embed / (y_embed[:, -1:, :] + eps) * self.scale
        x_embed = x_embed / (x_embed[:, :, -1:] + eps) * self.scale

        dim_t = torch.arange(
            self.hidden_dim, dtype=torch.float32, device=mask.device)
        dim_t = self.temperature**(2 * (dim_t // 2) / self.hidden_dim)

        pos_x = x_embed[:, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, None] / dim_t
        pos_x = torch.stack(
            (pos_x[:, :, :, 0::2].sin(), pos_x[:, :, :, 1::2].cos()),
            dim=4).flatten(3)
        pos_y = torch.stack(
            (pos_y[:, :, :, 0::2].sin(), pos_y[:, :, :, 1::2].cos()),
            dim=4).flatten(3)
        pos = torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)
        pos = self.token_projection(pos)
        return pos


def conv3x3(in_planes, out_planes, stride=1):
    """3x3 convolution with padding"""
    return torch.nn.Sequential(
        nn.Conv2d(
            in_planes,
            out_planes,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=False), nn.SyncBatchNorm(out_planes))


class ConvPatchEmbed(nn.Module):
    """ Image to Patch Embedding using multiple convolutional layers
    """

    def __init__(self, img_size=224, patch_size=16, in_chans=3, embed_dim=768):
        super().__init__()
        img_size = to_2tuple(img_size)
        patch_size = to_2tuple(patch_size)
        num_patches = (img_size[1] // patch_size[1]) * (
            img_size[0] // patch_size[0])
        self.img_size = img_size
        self.patch_size = patch_size
        self.num_patches = num_patches

        if patch_size[0] == 16:
            self.proj = torch.nn.Sequential(
                conv3x3(3, embed_dim // 8, 2),
                nn.GELU(),
                conv3x3(embed_dim // 8, embed_dim // 4, 2),
                nn.GELU(),
                conv3x3(embed_dim // 4, embed_dim // 2, 2),
                nn.GELU(),
                conv3x3(embed_dim // 2, embed_dim, 2),
            )
        elif patch_size[0] == 8:
            self.proj = torch.nn.Sequential(
                conv3x3(3, embed_dim // 4, 2),
                nn.GELU(),
                conv3x3(embed_dim // 4, embed_dim // 2, 2),
                nn.GELU(),
                conv3x3(embed_dim // 2, embed_dim, 2),
            )
        else:
            raise (
                'For convolutional projection, patch size has to be in [8, 16]'
            )

    def forward(self, x, padding_size=None):
        B, C, H, W = x.shape
        x = self.proj(x)
        Hp, Wp = x.shape[2], x.shape[3]
        x = x.flatten(2).transpose(1, 2)

        return x, (Hp, Wp)


class LPI(nn.Module):
    """
    Local Patch Interaction module that allows explicit communication between tokens in 3x3 windows
    to augment the implicit communcation performed by the block diagonal scatter attention.
    Implemented using 2 layers of separable 3x3 convolutions with GeLU and BatchNorm2d
    """

    def __init__(self,
                 in_features,
                 hidden_features=None,
                 out_features=None,
                 act_layer=nn.GELU,
                 drop=0.,
                 kernel_size=3):
        super().__init__()
        out_features = out_features or in_features

        padding = kernel_size // 2

        self.conv1 = torch.nn.Conv2d(
            in_features,
            out_features,
            kernel_size=kernel_size,
            padding=padding,
            groups=out_features)
        self.act = act_layer()
        self.bn = nn.SyncBatchNorm(in_features)
        self.conv2 = torch.nn.Conv2d(
            in_features,
            out_features,
            kernel_size=kernel_size,
            padding=padding,
            groups=out_features)

    def forward(self, x, H, W):
        B, N, C = x.shape
        x = x.permute(0, 2, 1).reshape(B, C, H, W)
        x = self.conv1(x)
        x = self.act(x)
        x = self.bn(x)
        x = self.conv2(x)
        x = x.reshape(B, C, N).permute(0, 2, 1)

        return x


class ClassAttention(nn.Module):
    """Class Attention Layer as in CaiT https://arxiv.org/abs/2103.17239
    """

    def __init__(self,
                 dim,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        head_dim = dim // num_heads
        self.scale = qk_scale or head_dim**-0.5

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads)
        qkv = qkv.permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[
            2]  # make torchscript happy (cannot use tensor as tuple)

        qc = q[:, :, 0:1]  # CLS token
        attn_cls = (qc * k).sum(dim=-1) * self.scale
        attn_cls = attn_cls.softmax(dim=-1)
        attn_cls = self.attn_drop(attn_cls)

        cls_tkn = (attn_cls.unsqueeze(2) @ v).transpose(1, 2).reshape(B, 1, C)
        cls_tkn = self.proj(cls_tkn)
        x = torch.cat([self.proj_drop(cls_tkn), x[:, 1:]], dim=1)
        return x


class ClassAttentionBlock(nn.Module):
    """Class Attention Layer as in CaiT https://arxiv.org/abs/2103.17239
    """

    def __init__(self,
                 dim,
                 num_heads,
                 mlp_ratio=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm,
                 eta=None,
                 tokens_norm=False):
        super().__init__()
        self.norm1 = norm_layer(dim)

        self.attn = ClassAttention(
            dim,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=drop)

        self.drop_path = DropPath(
            drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)
        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(
            in_features=dim,
            hidden_features=mlp_hidden_dim,
            act_layer=act_layer,
            drop=drop)

        if eta is not None:  # LayerScale Initialization (no layerscale when None)
            self.gamma1 = nn.Parameter(
                eta * torch.ones(dim), requires_grad=True)
            self.gamma2 = nn.Parameter(
                eta * torch.ones(dim), requires_grad=True)
        else:
            self.gamma1, self.gamma2 = 1.0, 1.0

        # FIXME: A hack for models pre-trained with layernorm over all the tokens not just the CLS
        self.tokens_norm = tokens_norm

    def forward(self, x, H, W, mask=None):
        x = x + self.drop_path(self.gamma1 * self.attn(self.norm1(x)))
        if self.tokens_norm:
            x = self.norm2(x)
        else:
            x[:, 0:1] = self.norm2(x[:, 0:1])

        x_res = x
        cls_token = x[:, 0:1]
        cls_token = self.gamma2 * self.mlp(cls_token)
        x = torch.cat([cls_token, x[:, 1:]], dim=1)
        x = x_res + self.drop_path(x)
        return x


class XCA(nn.Module):
    """ Cross-Covariance Attention (XCA) operation where the channels are updated using a weighted \
     sum.

        The weights are obtained from the (softmax normalized) Cross-covariance matrix
        (Q^T K \\in d_h \\times d_h)
    """

    def __init__(self,
                 dim,
                 num_heads=8,
                 qkv_bias=False,
                 qk_scale=None,
                 attn_drop=0.,
                 proj_drop=0.):
        super().__init__()
        self.num_heads = num_heads
        self.temperature = nn.Parameter(torch.ones(num_heads, 1, 1))

        self.qkv = nn.Linear(dim, dim * 3, bias=qkv_bias)
        self.attn_drop = nn.Dropout(attn_drop)
        self.proj = nn.Linear(dim, dim)
        self.proj_drop = nn.Dropout(proj_drop)

    def forward(self, x):
        B, N, C = x.shape
        qkv = self.qkv(x).reshape(B, N, 3, self.num_heads, C // self.num_heads)
        qkv = qkv.permute(2, 0, 3, 1, 4)
        q, k, v = qkv[0], qkv[1], qkv[
            2]  # make torchscript happy (cannot use tensor as tuple)

        q = q.transpose(-2, -1)
        k = k.transpose(-2, -1)
        v = v.transpose(-2, -1)

        q = torch.nn.functional.normalize(q, dim=-1)
        k = torch.nn.functional.normalize(k, dim=-1)

        attn = (q @ k.transpose(-2, -1)) * self.temperature
        attn = attn.softmax(dim=-1)
        attn = self.attn_drop(attn)

        x = (attn @ v).permute(0, 3, 1, 2).reshape(B, N, C)
        x = self.proj(x)
        x = self.proj_drop(x)
        return x

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'temperature'}


class XCABlock(nn.Module):

    def __init__(self,
                 dim,
                 num_heads,
                 mlp_ratio=4.,
                 qkv_bias=False,
                 qk_scale=None,
                 drop=0.,
                 attn_drop=0.,
                 drop_path=0.,
                 act_layer=nn.GELU,
                 norm_layer=nn.LayerNorm,
                 num_tokens=196,
                 eta=None):
        super().__init__()
        self.norm1 = norm_layer(dim)
        self.attn = XCA(
            dim,
            num_heads=num_heads,
            qkv_bias=qkv_bias,
            qk_scale=qk_scale,
            attn_drop=attn_drop,
            proj_drop=drop)
        self.drop_path = DropPath(
            drop_path) if drop_path > 0. else nn.Identity()
        self.norm2 = norm_layer(dim)

        mlp_hidden_dim = int(dim * mlp_ratio)
        self.mlp = Mlp(
            in_features=dim,
            hidden_features=mlp_hidden_dim,
            act_layer=act_layer,
            drop=drop)

        self.norm3 = norm_layer(dim)
        self.local_mp = LPI(in_features=dim, act_layer=act_layer)

        self.gamma1 = nn.Parameter(eta * torch.ones(dim), requires_grad=True)
        self.gamma2 = nn.Parameter(eta * torch.ones(dim), requires_grad=True)
        self.gamma3 = nn.Parameter(eta * torch.ones(dim), requires_grad=True)

    def forward(self, x, H, W):
        x = x + self.drop_path(self.gamma1 * self.attn(self.norm1(x)))
        x = x + self.drop_path(
            self.gamma3 * self.local_mp(self.norm3(x), H, W))
        x = x + self.drop_path(self.gamma2 * self.mlp(self.norm2(x)))
        return x


@BACKBONES.register_module
class XCiT(nn.Module):
    """
    Based on timm and DeiT code bases
    https://github.com/rwightman/pytorch-image-models/tree/master/timm
    https://github.com/facebookresearch/deit/
    """

    def __init__(self,
                 img_size=224,
                 patch_size=16,
                 in_chans=3,
                 num_classes=1000,
                 embed_dim=768,
                 depth=12,
                 num_heads=12,
                 mlp_ratio=4.,
                 qkv_bias=True,
                 qk_scale=None,
                 drop_rate=0.,
                 attn_drop_rate=0.,
                 drop_path_rate=0.,
                 norm_layer=None,
                 cls_attn_layers=2,
                 use_pos=True,
                 patch_proj='linear',
                 eta=None,
                 tokens_norm=False):
        """
        Args:
            img_size (int, tuple): input image size
            patch_size (int, tuple): patch size
            in_chans (int): number of input channels
            num_classes (int): number of classes for classification head
            embed_dim (int): embedding dimension
            depth (int): depth of transformer
            num_heads (int): number of attention heads
            mlp_ratio (int): ratio of mlp hidden dim to embedding dim
            qkv_bias (bool): enable bias for qkv if True
            qk_scale (float): override default qk scale of head_dim ** -0.5 if set
            drop_rate (float): dropout rate
            attn_drop_rate (float): attention dropout rate
            drop_path_rate (float): stochastic depth rate
            norm_layer: (nn.Module): normalization layer
            cls_attn_layers: (int) Depth of Class attention layers
            use_pos: (bool) whether to use positional encoding
            eta: (float) layerscale initialization value
            tokens_norm: (bool) Whether to normalize all tokens or just the cls_token in the CA
        """
        super().__init__()
        self.num_classes = num_classes
        self.num_features = self.embed_dim = embed_dim
        norm_layer = norm_layer or partial(nn.LayerNorm, eps=1e-6)

        self.patch_embed = ConvPatchEmbed(
            img_size=img_size, embed_dim=embed_dim, patch_size=patch_size)

        num_patches = self.patch_embed.num_patches

        self.cls_token = nn.Parameter(torch.zeros(1, 1, embed_dim))
        self.pos_drop = nn.Dropout(p=drop_rate)

        dpr = [drop_path_rate for i in range(depth)]
        self.blocks = nn.ModuleList([
            XCABlock(
                dim=embed_dim,
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                drop_path=dpr[i],
                norm_layer=norm_layer,
                num_tokens=num_patches,
                eta=eta) for i in range(depth)
        ])

        self.cls_attn_blocks = nn.ModuleList([
            ClassAttentionBlock(
                dim=embed_dim,
                num_heads=num_heads,
                mlp_ratio=mlp_ratio,
                qkv_bias=qkv_bias,
                qk_scale=qk_scale,
                drop=drop_rate,
                attn_drop=attn_drop_rate,
                norm_layer=norm_layer,
                eta=eta,
                tokens_norm=tokens_norm) for i in range(cls_attn_layers)
        ])
        self.norm = norm_layer(embed_dim)
        self.head = nn.Linear(
            self.num_features,
            num_classes) if num_classes > 0 else nn.Identity()

        self.pos_embeder = PositionalEncodingFourier(dim=embed_dim)
        self.use_pos = use_pos

        # Classifier head
        trunc_normal_(self.cls_token, std=.02)
        self.apply(self._init_weights)

    def _init_weights(self, m):
        if isinstance(m, nn.Linear):
            trunc_normal_(m.weight, std=.02)
            if isinstance(m, nn.Linear) and m.bias is not None:
                nn.init.constant_(m.bias, 0)
        elif isinstance(m, nn.LayerNorm):
            nn.init.constant_(m.bias, 0)
            nn.init.constant_(m.weight, 1.0)

    def init_weights(self, pretrained=None):
        if isinstance(pretrained, str) or isinstance(pretrained, dict):
            logger = get_root_logger()
            load_checkpoint(
                self,
                pretrained,
                map_location='cpu',
                strict=False,
                logger=logger)
        elif pretrained is None:
            self.apply(self._init_weights)
        else:
            raise TypeError('pretrained must be a str or None')

    @torch.jit.ignore
    def no_weight_decay(self):
        return {'pos_embed', 'cls_token', 'dist_token'}

    def forward_features(self, x):
        B, C, H, W = x.shape

        x, (Hp, Wp) = self.patch_embed(x)

        if self.use_pos:
            pos_encoding = self.pos_embeder(B, Hp, Wp).reshape(
                B, -1, x.shape[1]).permute(0, 2, 1)
            x = x + pos_encoding

        x = self.pos_drop(x)

        for blk in self.blocks:
            x = blk(x, Hp, Wp)

        cls_tokens = self.cls_token.expand(B, -1, -1)
        x = torch.cat((cls_tokens, x), dim=1)

        for blk in self.cls_attn_blocks:
            x = blk(x, Hp, Wp)

        x = self.norm(x)[:, 0]
        return x

    def forward(self, x):
        x = self.forward_features(x)
        x = self.head(x)

        if self.training:
            return x, x
        else:
            return x


# # Patch size 16x16 models
# @register_model
# def xcit_nano_12_p16(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=16, embed_dim=128, depth=12, num_heads=4, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1.0, tokens_norm=False, **kwargs)
#     model.default_cfg = _cfg()
#     return model

# @register_model
# def xcit_tiny_12_p16(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=16, embed_dim=192, depth=12, num_heads=4, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1.0, tokens_norm=True, **kwargs)
#     model.default_cfg = _cfg()
#     return model


# @register_model
def xcit_small_12_p16(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=16,
        embed_dim=384,
        depth=12,
        num_heads=8,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1.0,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model


# @register_model
# def xcit_tiny_24_p16(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=16, embed_dim=192, depth=24, num_heads=4, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1e-5, tokens_norm=True, **kwargs)
#     model.default_cfg = _cfg()
#     return model


def xcit_small_24_p16(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=16,
        embed_dim=384,
        depth=24,
        num_heads=8,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1e-5,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model


def xcit_medium_24_p16(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=16,
        embed_dim=512,
        depth=24,
        num_heads=8,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1e-5,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model


# @register_model
# def xcit_large_24_p16(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=16, embed_dim=768, depth=24, num_heads=16, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1e-5, tokens_norm=True, **kwargs)
#     model.default_cfg = _cfg()
#     return model

# # Patch size 8x8 models
# @register_model
# def xcit_nano_12_p8(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=8, embed_dim=128, depth=12, num_heads=4, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1.0, tokens_norm=False, **kwargs)
#     model.default_cfg = _cfg()
#     return model

# @register_model
# def xcit_tiny_12_p8(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=8, embed_dim=192, depth=12, num_heads=4, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1.0, tokens_norm=True, **kwargs)
#     model.default_cfg = _cfg()
#     return model


# @register_model
def xcit_small_12_p8(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=8,
        embed_dim=384,
        depth=12,
        num_heads=8,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1.0,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model


# @register_model
# def xcit_tiny_24_p8(pretrained=False, **kwargs):
#     model = XCiT(
#         patch_size=8, embed_dim=192, depth=24, num_heads=4, mlp_ratio=4, qkv_bias=True,
#         norm_layer=partial(nn.LayerNorm, eps=1e-6), eta=1e-5, tokens_norm=True, **kwargs)
#     model.default_cfg = _cfg()
#     return model


def xcit_small_24_p8(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=8,
        embed_dim=384,
        depth=24,
        num_heads=8,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1e-5,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model


def xcit_medium_24_p8(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=8,
        embed_dim=512,
        depth=24,
        num_heads=8,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1e-5,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model


# @register_model
def xcit_large_24_p8(pretrained=False, **kwargs):
    model = XCiT(
        patch_size=8,
        embed_dim=768,
        depth=24,
        num_heads=16,
        mlp_ratio=4,
        qkv_bias=True,
        norm_layer=partial(nn.LayerNorm, eps=1e-6),
        eta=1e-5,
        tokens_norm=True,
        **kwargs)
    model.default_cfg = _cfg()
    return model
