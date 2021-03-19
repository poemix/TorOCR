import torch.nn as nn

from addict import Dict

from torocr.layers.activation import HSwish, HSigmoid


class ConvBNACT(nn.Module):
    def __init__(self, in_channels, out_channels, kernel_size, stride=1, padding=0, groups=1, act=None):
        super(ConvBNACT, self).__init__()
        self.conv = nn.Conv2d(
            in_channels=in_channels, out_channels=out_channels, kernel_size=kernel_size,
            stride=stride, padding=padding, groups=groups, bias=False
        )
        self.bn = nn.BatchNorm2d(out_channels)
        if act == "relu":
            self.act = nn.ReLU()
        elif act == "hard_swish":
            self.act = HSwish()
        elif act is None:
            self.act = None

    def forward(self, x):
        x = self.conv(x)
        x = self.bn(x)
        if self.act is not None:
            x = self.act(x)
        return x


class SEBlock(nn.Module):
    def __init__(self, in_channels, out_channels, ratio=4):
        super(SEBlock, self).__init__()

        num_mid_filter = out_channels // ratio
        self.pool = nn.AdaptiveAvgPool2d(1)
        self.conv1 = nn.Conv2d(in_channels=in_channels, out_channels=num_mid_filter, kernel_size=1, bias=True)
        self.relu1 = nn.ReLU()
        self.conv2 = nn.Conv2d(in_channels=num_mid_filter, kernel_size=1, out_channels=out_channels, bias=True)
        self.relu2 = HSigmoid()

    def forward(self, x):
        attn = self.pool(x)
        attn = self.conv1(attn)

        attn = self.relu1(attn)
        attn = self.conv2(attn)
        attn = self.relu2(attn)
        return x * attn


class ResidualUnit(nn.Module):
    def __init__(self, num_in_filter, num_mid_filter, num_out_filter, stride, kernel_size, act=None, use_se=False):
        super(ResidualUnit, self).__init__()
        self.conv0 = ConvBNACT(
            in_channels=num_in_filter, out_channels=num_mid_filter, kernel_size=1, stride=1, padding=0, act=act
        )

        self.conv1 = ConvBNACT(
            in_channels=num_mid_filter, out_channels=num_mid_filter, kernel_size=kernel_size, stride=stride,
            padding=int((kernel_size - 1) // 2), act=act, groups=num_mid_filter
        )
        if use_se:
            self.se = SEBlock(in_channels=num_mid_filter, out_channels=num_mid_filter)
        else:
            self.se = None

        self.conv2 = ConvBNACT(
            in_channels=num_mid_filter, out_channels=num_out_filter, kernel_size=1, stride=1, padding=0
        )
        self.not_add = num_in_filter != num_out_filter or stride != 1

    def forward(self, x):
        y = self.conv0(x)
        y = self.conv1(y)
        if self.se is not None:
            y = self.se(y)
        y = self.conv2(y)
        if not self.not_add:
            y = x + y
        return y


class MobileNetV3(nn.Module):
    def __init__(self, config=None):
        super(MobileNetV3, self).__init__()

        self.cfg = Dict(config)

        self.in_channels = self.cfg.get("in_channels", 3)
        self.scale = self.cfg.get("scale", 0.5)
        model_name = self.cfg.get("model_name", "small")
        self.inplanes = 16
        if model_name == "large":
            cfg = [
                # k, exp, c,  se,     nl,  s,
                [3, 16, 16, False, "relu", 1],
                [3, 64, 24, False, "relu", (2, 1)],
                [3, 72, 24, False, "relu", 1],
                [5, 72, 40, True, "relu", (2, 1)],
                [5, 120, 40, True, "relu", 1],
                [5, 120, 40, True, "relu", 1],
                [3, 240, 80, False, "hard_swish", 1],
                [3, 200, 80, False, "hard_swish", 1],
                [3, 184, 80, False, "hard_swish", 1],
                [3, 184, 80, False, "hard_swish", 1],
                [3, 480, 112, True, "hard_swish", 1],
                [3, 672, 112, True, "hard_swish", 1],
                [5, 672, 160, True, "hard_swish", (2, 1)],
                [5, 960, 160, True, "hard_swish", 1],
                [5, 960, 160, True, "hard_swish", 1],
            ]
            self.cls_ch_squeeze = 960
            self.cls_ch_expand = 1280
        elif model_name == "small":
            cfg = [
                # k, exp, c,  se,     nl,  s,
                [3, 16, 16, True, "relu", (1, 1)],
                [3, 72, 24, False, "relu", (2, 1)],
                [3, 88, 24, False, "relu", 1],
                [5, 96, 40, True, "hard_swish", (2, 1)],
                [5, 240, 40, True, "hard_swish", 1],
                [5, 240, 40, True, "hard_swish", 1],
                [5, 120, 48, True, "hard_swish", 1],
                [5, 144, 48, True, "hard_swish", 1],
                [5, 288, 96, True, "hard_swish", (2, 1)],
                [5, 576, 96, True, "hard_swish", 1],
                [5, 576, 96, True, "hard_swish", 1],
            ]
            self.cls_ch_squeeze = 576
            self.cls_ch_expand = 1280
        else:
            raise NotImplementedError(f"mode[{model_name}_model] is not implemented!")

        supported_scale = [0.35, 0.5, 0.75, 1.0, 1.25]
        assert self.scale in supported_scale, f"supported scale are {supported_scale} but input scale is {self.scale}"

        scale = self.scale
        inplanes = self.inplanes
        cls_ch_squeeze = self.cls_ch_squeeze
        self.conv1 = ConvBNACT(
            in_channels=self.in_channels,
            out_channels=self.make_divisible(inplanes * scale),
            kernel_size=3,
            stride=2,
            padding=1,
            groups=1,
            act="hard_swish"
        )
        inplanes = self.make_divisible(inplanes * scale)
        block_list = []
        for layer_cfg in cfg:
            block = ResidualUnit(
                num_in_filter=inplanes,
                num_mid_filter=self.make_divisible(scale * layer_cfg[1]),
                num_out_filter=self.make_divisible(scale * layer_cfg[2]),
                act=layer_cfg[4],
                stride=layer_cfg[5],
                kernel_size=layer_cfg[0],
                use_se=layer_cfg[3]
            )
            block_list.append(block)
            inplanes = self.make_divisible(scale * layer_cfg[2])

        self.block_list = nn.Sequential(*block_list)
        self.conv2 = ConvBNACT(
            in_channels=inplanes,
            out_channels=self.make_divisible(scale * cls_ch_squeeze),
            kernel_size=1,
            stride=1,
            padding=0,
            groups=1,
            act="hard_swish"
        )

        self.pool = nn.MaxPool2d(kernel_size=2, stride=2, padding=0)
        self.out_channels = self.make_divisible(scale * cls_ch_squeeze)

    def make_divisible(self, v, divisor=8, min_value=None):
        if min_value is None:
            min_value = divisor
        new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
        if new_v < 0.9 * v:
            new_v += divisor
        return new_v

    def forward(self, x):
        x = self.conv1(x)
        x = self.block_list(x)
        x = self.conv2(x)
        x = self.pool(x)
        return x