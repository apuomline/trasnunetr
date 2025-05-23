import torch
from torch import nn
from torch.nn import functional as F
from einops.layers.torch import Rearrange
from einops import rearrange
from thop import profile
def pair(t):
    return t if isinstance(t, tuple) else (t, t)

class PreNorm(nn.Module):
    def __init__(self, dim, fn):
        super().__init__()
        self.norm = nn.LayerNorm(dim)
        self.fn = fn
    def forward(self, x, **kwargs):
        return self.fn(self.norm(x), **kwargs)

class FeedForward(nn.Module):
    def __init__(self, dim, hidden_dim, dropout = 0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, dim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)

class DenseForward(nn.Module):
    def __init__(self, dim, hidden_dim, outdim, dropout=0.):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(dim, hidden_dim),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(hidden_dim, outdim),
            nn.Dropout(dropout)
        )
    def forward(self, x):
        return self.net(x)


class Dense_Attention(nn.Module):
    def __init__(self, dim, heads=8, dim_head=64, dropout=0., num_patches=None):
        super().__init__()
        inner_dim = dim_head *  heads
        project_out = not (heads == 1 and dim_head == dim)

        self.heads = heads
        self.scale = dim_head ** -0.5

        self.attend = nn.Softmax(dim = -1)
        self.to_qkv = nn.Linear(dim, inner_dim * 3, bias = False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, dim),
            nn.Dropout(dropout)
        ) if project_out else nn.Identity()

    def forward(self, x):
        # x = torch.cat(x, 2)
        qkv = self.to_qkv(x).chunk(3, dim = -1)
        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> b h n d', h = self.heads), qkv)

        dots = torch.matmul(q, k.transpose(-1, -2)) * self.scale

        attn = self.attend(dots)

        out = torch.matmul(attn, v)
        out = rearrange(out, 'b h n d -> b n (h d)')
        return self.to_out(out)


class DensePreConv_AttentionBlock(nn.Module):
    def __init__(self, out_channels, height, width, growth_rate=32,  depth=4, heads=8,  dropout=0.5, attention=Dense_Attention):
        super().__init__()
        mlp_dim = growth_rate * 2
        self.layers = nn.ModuleList([])
        for i in range(depth):
            self.layers.append(nn.ModuleList([
                nn.Linear(out_channels + i * growth_rate, growth_rate),
                PreNorm(growth_rate, attention(growth_rate, heads = heads, dim_head = (growth_rate) // heads, dropout = dropout, num_patches=(height,width))),
                PreNorm(growth_rate, DenseForward(growth_rate, mlp_dim,growth_rate, dropout = dropout))
            ]))
        self.out_layer = DenseForward(out_channels + depth * growth_rate, mlp_dim,out_channels, dropout = dropout)
            
    def forward(self, x):
        features = [x]
        for l, attn, ff in self.layers:
            x = torch.cat(features, 2)
            x = l(x)
            x = attn(x) + x
            x = ff(x) + x
            features.append(ff(x))
        x = torch.cat(features, 2)
        x = self.out_layer(x)
        return x


class Dense_TransformerBlock(nn.Module):
    def __init__(self, in_channels, out_channels, image_size, growth_rate=32, patch_size=16, depth=6, heads=8, dropout=0.5, attention=DensePreConv_AttentionBlock):
        super().__init__()
        image_height, image_width = pair(image_size)
        patch_height, patch_width = pair(patch_size)

        self.outsize = (image_height // patch_size, image_width// patch_size)
        h = image_height // patch_height
        w = image_width // patch_width
        num_patches = (image_height // patch_height) * (image_width // patch_width)
        mlp_dim = out_channels * 2
        self.patch_embeddings = nn.Conv2d(in_channels=in_channels,
                                       out_channels=out_channels,
                                       kernel_size=patch_size,
                                       stride=patch_size)
        self.position_embeddings = nn.Parameter(torch.zeros(1, num_patches, out_channels))

        self.blocks = nn.ModuleList([])
        for i in range(depth):
            self.blocks.append(nn.ModuleList([
                attention(out_channels, height=h, width=w, growth_rate=growth_rate)
            ]))
        
        self.re_patch_embedding = nn.Sequential(
            Rearrange('b (h w) (p1 p2 c) -> b c (h p1) (w p2)', p1 = 1, p2 = 1, h = h)
        )
        self.dropout = nn.Dropout(dropout)
        
    def forward(self, img):
        x = self.patch_embeddings(img)  # (B, hidden. n_patches^(1/2), n_patches^(1/2))
        x = x.flatten(2)
        x = x.transpose(-1, -2)  # (B, n_patches, hidden)
        embeddings = x + self.position_embeddings
        x = self.dropout(embeddings)

        for block, in self.blocks:
            # print(block)
            x = block(x)
        
        x = self.re_patch_embedding(x)
        return F.interpolate(x, self.outsize)


class BasicConv2d(nn.Module):
    def __init__(self, in_channels, out_channels, **kwargs):
        super(BasicConv2d, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, bias=False, **kwargs)
        self.norm = nn.InstanceNorm2d(out_channels, affine=True)

    def forward(self, x):
        x = self.conv(x)
        x = self.norm(x)
        x = F.relu(x, inplace=True)
        return x

class UpConv(nn.Module):
    def __init__(self, in_channels, out_channels, scale=2):
        super().__init__()
        self.scale = scale
        self.double_conv = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1),
            nn.InstanceNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x):
        x = self.double_conv(x)
        x = F.interpolate(x, scale_factor=self.scale, mode='bilinear', align_corners=False)
        return x


class HDenseFormer_2D(nn.Module):
    def __init__(self, in_channels, n_cls, n_filters, img_size=(384,384), transformer_depth=12):
        super(HDenseFormer_2D, self).__init__()
        self.in_channels = in_channels
        self.n_cls = n_cls
        self.n_filters = n_filters

        self.attns = nn.ModuleList(
            [Dense_TransformerBlock(in_channels=1,out_channels=4 * n_filters,image_size=img_size,
            patch_size=16,depth=transformer_depth//4,attention=DensePreConv_AttentionBlock) for _ in range(self.in_channels)] 
            )

        # self.deep_conv = BasicConv2d(4 * n_filters * 3, 8 * n_filters, kernel_size=3, stride=1, padding=1)
        self.deep_conv = UpConv(4 * n_filters * self.in_channels, 8 * n_filters)

        self.up1 = UpConv(8 * n_filters,4 * n_filters)
        self.up2 = UpConv(4 * n_filters,2 * n_filters)
        self.up3 = UpConv(2 * n_filters,1 * n_filters)

        self.block_1_1_left = BasicConv2d(in_channels, n_filters, kernel_size=3, stride=1, padding=1)
        self.block_1_2_left = BasicConv2d(n_filters, n_filters, kernel_size=3, stride=1, padding=1)

        self.pool_1 = nn.MaxPool2d(kernel_size=2, stride=2)  # 64, 1/2
        self.block_2_1_left = BasicConv2d(n_filters, 2 * n_filters, kernel_size=3, stride=1, padding=1)
        self.block_2_2_left = BasicConv2d(2 * n_filters, 2 * n_filters, kernel_size=3, stride=1, padding=1)

        self.pool_2 = nn.MaxPool2d(kernel_size=2, stride=2)  # 128, 1/4
        self.block_3_1_left = BasicConv2d(2 * n_filters, 4 * n_filters, kernel_size=3, stride=1, padding=1)
        self.block_3_2_left = BasicConv2d(4 * n_filters, 4 * n_filters, kernel_size=3, stride=1, padding=1)

        self.pool_3 = nn.MaxPool2d(kernel_size=2, stride=2)  # 256, 1/8
        self.block_4_1_left = BasicConv2d(4 * n_filters, 8 * n_filters, kernel_size=3, stride=1, padding=1)
        self.block_4_2_left = BasicConv2d(8 * n_filters, 8 * n_filters, kernel_size=3, stride=1, padding=1)

        self.upconv_3 = nn.ConvTranspose2d(8 * n_filters, 4 * n_filters, kernel_size=3, stride=2, padding=1, output_padding=1)
        self.block_3_1_right = BasicConv2d((4 + 4) * n_filters, 4 * n_filters, kernel_size=3, stride=1, padding=1)
        self.block_3_2_right = BasicConv2d(4 * n_filters, 4 * n_filters, kernel_size=3, stride=1, padding=1)

        self.upconv_2 = nn.ConvTranspose2d(4 * n_filters, 2 * n_filters, kernel_size=3, stride=2, padding=1, output_padding=1)
        self.block_2_1_right = BasicConv2d((2 + 2) * n_filters, 2 * n_filters, kernel_size=3, stride=1, padding=1)
        self.block_2_2_right = BasicConv2d(2 * n_filters, 2 * n_filters, kernel_size=3, stride=1, padding=1)

        self.upconv_1 = nn.ConvTranspose2d(2 * n_filters, n_filters, kernel_size=3, stride=2, padding=1, output_padding=1)
        self.block_1_1_right = BasicConv2d((1 + 1) * n_filters, n_filters, kernel_size=3, stride=1, padding=1)
        self.block_1_2_right = BasicConv2d(n_filters, n_filters, kernel_size=3, stride=1, padding=1)

        self.conv1x1 = nn.Conv2d(n_filters, self.n_cls, kernel_size=1, stride=1, padding=0)

        self.conv1x1_d1 = nn.Conv2d(2 * n_filters, self.n_cls, kernel_size=1, stride=1, padding=0)
        self.conv1x1_d2 = nn.Conv2d(4 * n_filters, self.n_cls, kernel_size=1, stride=1, padding=0)
        self.conv1x1_d3 = nn.Conv2d(8 * n_filters, self.n_cls, kernel_size=1, stride=1, padding=0)

    def forward(self, x):
        attnall = torch.cat([self.attns[i](x[:,i:i+1,:,:]) for i in range(self.in_channels)],1)
        attnout = self.deep_conv(attnall)  # 256, 1/8

        at1 = self.up1(attnout)   # 128, 1/4
        at2 = self.up2(at1)  # 64, 1/2
        at3 = self.up3(at2)

        ds0 = self.block_1_2_left(self.block_1_1_left(x))
        ds0 = ds0+at3
        ds1 = self.block_2_2_left(self.block_2_1_left(self.pool_1(ds0)))
        ds1 = ds1+at2
        ds2 = self.block_3_2_left(self.block_3_1_left(self.pool_2(ds1)))
        ds2 = ds2+at1
        x = self.block_4_2_left(self.block_4_1_left(self.pool_3(ds2)))
        x = x+attnout

        out3 = self.conv1x1_d3(x)
        x = self.block_3_2_right(self.block_3_1_right(torch.cat([self.upconv_3(x), ds2], 1)))
        out2 = self.conv1x1_d2(x)
        x = self.block_2_2_right(self.block_2_1_right(torch.cat([self.upconv_2(x), ds1], 1)))
        out1 = self.conv1x1_d1(x)
        x = self.block_1_2_right(self.block_1_1_right(torch.cat([self.upconv_1(x), ds0], 1)))

        x = self.conv1x1(x)
        # return [x,out1,out2,out3] ###如何将深度监督这种东西应用上?
        return x

def Rolling_Unet_S(num_classes, input_channels, deep_supervision,img_size, transformer_depth=6):
    return HDenseFormer_2D(in_channels=input_channels, n_cls=num_classes, img_size=img_size,n_filters=32, transformer_depth=transformer_depth)

def HDenseFormer_2D_16(in_channels, n_cls, image_size, transformer_depth):
    return HDenseFormer_2D(in_channels=in_channels, n_cls=n_cls, img_size=img_size, n_filters=16,transformer_depth=transformer_depth)



if __name__=='__main__':
     # print('1')
   
    input=torch.rand(1,3,512,512)
    model=Rolling_Unet_S(1,3,False,512)
    out=model(input)
    # print(f'out.shape:{out.shape}')
    flops, params = profile(model, inputs=(input,))
    print('FLOPs = ' + str(flops / 1000 ** 3) + 'G')
    print('Params = ' + str(params / 1000 ** 2) + 'M')
