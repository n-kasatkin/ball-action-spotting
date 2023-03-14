"""
Single-stage model combining 2.5D and 3D data to properly extract temporal information
from the video data.
Original idea:
https://www.kaggle.com/competitions/dfl-bundesliga-data-shootout/discussion/359932
"""

from torch import nn

import timm
from timm.models.layers import (
    DropPath,
    create_conv2d,
    create_classifier,
    get_act_layer,
    get_norm_act_layer,
)


class BatchNormAct3d(nn.Module):
    def __init__(self,
                 num_features: int,
                 act_layer=nn.ReLU,
                 apply_act: bool = True,
                 inplace_act: bool = True):
        super().__init__()
        self.bn3d = nn.BatchNorm3d(num_features)
        if apply_act:
            self.act = act_layer(inplace=inplace_act)
        else:
            self.act = nn.Identity()

    def forward(self, x):
        x = self.bn3d(x)
        x = self.act(x)
        return x


class SqueezeExcite(nn.Module):
    def __init__(self,
                 in_features: int,
                 reduce_ratio: int = 8,
                 act_layer=nn.ReLU,
                 gate_layer=nn.Sigmoid):
        super().__init__()
        rd_channels = in_features // reduce_ratio
        self.conv_reduce = nn.Conv3d(in_features, rd_channels, (1, 1, 1), bias=True)
        self.act1 = act_layer(inplace=True)
        self.conv_expand = nn.Conv3d(rd_channels, in_features, (1, 1, 1), bias=True)
        self.gate = gate_layer()

    def forward(self, x):
        x_se = x.mean((2, 3, 4), keepdim=True)
        x_se = self.conv_reduce(x_se)
        x_se = self.act1(x_se)
        x_se = self.conv_expand(x_se)
        return x * self.gate(x_se)


class InvertedResidual3d(nn.Module):
    def __init__(self,
                 in_features: int,
                 out_features: int,
                 expansion_ratio: int = 6,
                 se_reduce_ratio: int = 24,
                 act_layer=nn.ReLU,
                 drop_path_rate: float = 0.,
                 bias: bool = False):
        super().__init__()
        mid_features = in_features * expansion_ratio

        # Point-wise expansion
        self.conv_pw = nn.Conv3d(in_features, mid_features, (1, 1, 1), bias=bias)
        self.bn1 = BatchNormAct3d(mid_features, act_layer=act_layer)

        # Depth-wise convolution
        self.conv_dw = nn.Conv3d(mid_features, mid_features,
                                 kernel_size=(3, 3, 3), stride=(1, 1, 1),
                                 dilation=(1, 1, 1), padding=(1, 1, 1),
                                 groups=mid_features, bias=bias)
        self.bn2 = BatchNormAct3d(mid_features, act_layer=act_layer)

        # Squeeze-and-excitation
        self.se = SqueezeExcite(mid_features, act_layer=act_layer, reduce_ratio=se_reduce_ratio)

        # Point-wise linear projection
        self.conv_pwl = nn.Conv3d(mid_features, out_features, (1, 1, 1), bias=bias)
        self.bn3 = BatchNormAct3d(out_features, apply_act=False)
        self.drop_path = DropPath(drop_path_rate) if drop_path_rate else nn.Identity()

    def forward(self, x):
        shortcut = x
        x = self.conv_pw(x)
        x = self.bn1(x)
        x = self.conv_dw(x)
        x = self.bn2(x)
        x = self.se(x)
        x = self.conv_pwl(x)
        x = self.bn3(x)
        x = self.drop_path(x) + shortcut
        return x


class MultiDimStacker(nn.Module):
    def __init__(self,
                 model_name: str,
                 num_classes: int,
                 num_frames: int = 15,
                 stack_size: int = 3,
                 index_2d_features: int = 4,
                 pretrained: bool = False,
                 num_3d_blocks: int = 2,
                 num_3d_features: int = 192,
                 num_3d_stack_proj: int = 256,
                 expansion_3d_ratio: int = 6,
                 se_reduce_3d_ratio: int = 24,
                 drop_rate: bool = 0.,
                 drop_path_rate: float = 0.,
                 act_layer: str = "silu",
                 **kwargs):
        super().__init__()
        assert num_frames > 0 and num_frames % 3 == 0
        self.num_frames = num_frames
        self.stack_size = stack_size
        self.num_3d_features = num_3d_features
        self.num_stacks = num_frames // stack_size
        self.num_features = num_3d_stack_proj * self.num_stacks
        self.drop_rate = drop_rate

        act_layer = get_act_layer(act_layer)
        norm_act_layer = get_norm_act_layer(nn.BatchNorm2d, act_layer)

        self.conv2d_encoder = timm.create_model(
            model_name,
            pretrained=pretrained,
            num_classes=num_classes,
            in_chans=stack_size,
            drop_rate=drop_rate,
            drop_path_rate=drop_path_rate,
            features_only=True,
            out_indices=[index_2d_features],
            **kwargs
        )

        self.conv2d_projection = nn.Sequential(
            create_conv2d(
                self.conv2d_encoder.feature_info[index_2d_features]["num_chs"],
                num_3d_features,
                kernel_size=1, stride=1,
            ),
            norm_act_layer(num_3d_features, inplace=True)
        )

        self.conv3d_encoder = nn.Sequential(*[
            InvertedResidual3d(
                num_3d_features,
                num_3d_features,
                expansion_ratio=expansion_3d_ratio,
                se_reduce_ratio=se_reduce_3d_ratio,
                act_layer=act_layer,
                drop_path_rate=drop_path_rate,
            ) for _ in range(num_3d_blocks)
        ])

        self.conv3d_projection = nn.Sequential(
            create_conv2d(
                num_3d_features,
                num_3d_stack_proj,
                kernel_size=1, stride=1,
            ),
            norm_act_layer(num_3d_stack_proj, inplace=True),
        )

        self.global_pool, self.classifier = create_classifier(
            self.num_features, num_classes, pool_type="avg"
        )

    def forward_2d(self, frames):
        b, t, h, w = frames.shape  # (2, 15, 736, 1280)
        assert t % self.stack_size == 0
        num_stacks = t // self.stack_size
        stacked_frames = frames.view(
            b * num_stacks, self.stack_size, h, w
        )  # (10, 3, 736, 1280)
        conv2d_features = self.conv2d_encoder(
            stacked_frames
        )[-1]  # (10, 1280, 23, 40)
        conv2d_features = self.conv2d_projection(conv2d_features)  # (10, 512, 23, 40)
        _, _, h, w = conv2d_features.shape
        conv2d_features = conv2d_features.contiguous().view(
            b, self.num_3d_features, num_stacks, h, w
        )  # (2, 512, 5, 23, 40)
        return conv2d_features

    def forward_3d(self, conv2d_features):
        b, c, t, h, w = conv2d_features.shape  # (2, 512, 5, 23, 40)
        assert c == self.num_3d_features and t == self.num_stacks
        conv3d_features = self.conv3d_encoder(conv2d_features)  # (2, 512, 5, 23, 40)
        conv3d_features = conv3d_features.view(b * t, c, h, w)  # (10, 512, 23, 40)
        conv3d_features = self.conv3d_projection(conv3d_features)  # (10, 256, 23, 40)
        conv3d_features = conv3d_features.view(
            b, self.num_features, h, w
        )  # (2, 1280, 23, 40)
        return conv3d_features

    def forward_head(self, x):
        x = self.global_pool(x)
        if self.drop_rate > 0.:
            x = nn.functional.dropout(x, p=self.drop_rate, training=self.training)
        x = self.classifier(x)
        return x

    def forward(self, x):
        x = self.forward_2d(x)
        x = self.forward_3d(x)
        x = self.forward_head(x)
        return x