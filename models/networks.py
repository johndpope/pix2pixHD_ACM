from torchvision import models
import torch
import torch.nn as nn
import functools
from torch.autograd import Variable
import torch.nn.functional as F
import numpy as np

from models.layers import SNConv2d

from torchgan.layers import SelfAttention2d

###############################################################################
# Functions
###############################################################################


def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm2d') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)


def get_norm_layer(norm_type='instance'):
    if norm_type == 'batch':
        norm_layer = functools.partial(nn.BatchNorm2d, affine=True)
    elif norm_type == 'instance':
        norm_layer = functools.partial(nn.InstanceNorm2d, affine=False)
    elif norm_type == 'adain':
        norm_layer = AdaptiveInstanceNorm2d
    else:
        raise NotImplementedError(
            'normalization layer [%s] is not found' % norm_type)
    return norm_layer


class Mish(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        # inlining this saves 1 second per epoch (V100 GPU) vs having a temp x and then returning x(!)
        return x * (torch.tanh(F.softplus(x)))

##################################################################################
# Normalization layers
##################################################################################


class AdaptiveInstanceNorm2d(nn.Module):
    def __init__(self, num_features, eps=1e-5, momentum=0.1):
        super(AdaptiveInstanceNorm2d, self).__init__()
        self.num_features = num_features
        self.eps = eps
        self.momentum = momentum
        # weight and bias are dynamically assigned
        self.weight = None
        self.bias = None
        # just dummy buffers, not used
        self.register_buffer('running_mean', torch.zeros(num_features))
        self.register_buffer('running_var', torch.ones(num_features))

    def forward(self, x):
        # assert self.weight is not None and self.bias is not None, "Please assign weight and bias before calling AdaIN!"
        b, c = x.size(0), x.size(1)
        running_mean = self.running_mean.repeat(b)
        running_var = self.running_var.repeat(b)

        # Apply instance norm
        x_reshaped = x.contiguous().view(1, b * c, *x.size()[2:])

        out = F.batch_norm(
            x_reshaped, running_mean, running_var, self.weight, self.bias,
            True, self.momentum, self.eps)

        return out.view(b, c, *x.size()[2:])

    def __repr__(self):
        return self.__class__.__name__ + '(' + str(self.num_features) + ')'


def define_G(input_nc, output_nc, ngf, netG, n_downsample_global=3, n_blocks_global=9, n_local_enhancers=1,
             n_blocks_local=3, norm='instance', cond=False, n_self_attention=1, img_size=512, vocab_size=512,
             gpu_ids=[]):
    norm_layer = get_norm_layer(norm_type=norm)
    if netG == 'global':
        netG = GlobalGenerator(input_nc, output_nc, ngf, n_downsample_global, n_blocks_global, norm_layer, cond=cond,
                               n_self_attention=n_self_attention, img_size=img_size, vocab_size=vocab_size)
    elif netG == 'local':
        netG = LocalEnhancer(input_nc, output_nc, ngf, n_downsample_global, n_blocks_global,
                             n_local_enhancers, n_blocks_local, norm_layer, cond=cond,
                             n_self_attention=n_self_attention, img_size=img_size)
    elif netG == 'encoder':
        netG = Encoder(input_nc, output_nc, ngf,
                       n_downsample_global, norm_layer, img_size=img_size)
    else:
        raise('generator not implemented!')
    print(netG)
    if len(gpu_ids) > 0:
        assert(torch.cuda.is_available())
        netG.cuda(gpu_ids[0])
    netG.apply(weights_init)
    return netG


def define_D(input_nc, ndf, n_layers_D, norm='instance', use_sigmoid=False, num_D=1, getIntermFeat=False, n_self_attention=1, gpu_ids=[]):
    norm_layer = get_norm_layer(norm_type=norm)
    netD = MultiscaleDiscriminator(
        input_nc, ndf, n_layers_D, norm_layer, use_sigmoid, num_D, getIntermFeat, n_self_attention)
    print(netD)
    if len(gpu_ids) > 0:
        assert(torch.cuda.is_available())
        netD.cuda(gpu_ids[0])
    netD.apply(weights_init)
    return netD


def print_network(net):
    if isinstance(net, list):
        net = net[0]
    num_params = 0
    for param in net.parameters():
        num_params += param.numel()
    print(net)
    print('Total number of parameters: %d' % num_params)

##############
# ACM module
###############


def conv3x3(in_planes, out_planes):
    "3x3 convolution with padding"
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=1,
                     padding=1, bias=False)


class Reshape(nn.Module):
    def __init__(self, *args):
        super(Reshape, self).__init__()
        self.shape = args

    def forward(self, x):
        # print(x.size())
        return x.view(-1, *self.shape)


# The implementation of ACM (affine combination module)
class ACM(nn.Module):
    def __init__(self, channel_num, gf_dim=3, img_size=256, vocab_size=512):
        super(ACM, self).__init__()
        self.ngf = channel_num
        self.conv = conv3x3(gf_dim, 128)
        self.conv_weight = conv3x3(128, gf_dim)    # weight
        self.conv_bias = conv3x3(128, gf_dim)      # bias

        self.img_size = img_size

        text_encoder = [nn.Linear(vocab_size, img_size)]

        self.txt_encoder = nn.Sequential(*text_encoder)

    def forward(self, labels, img):
        # print("ACM forward...")
        # print(labels.size())
        # print(img.size())

        # print("ACM types...")
        # print(labels.type())
        # print(img.type())

        out_code = self.conv(img)
        out_code_weight = self.conv_weight(out_code)
        out_code_bias = self.conv_bias(out_code)

        # pad_len = out_code_weight.size(3) - labels.size(2)

        # padding = torch.zeros((labels.size(0), 1, pad_len),
        #                      device=labels.device,
        #                      dtype=torch.float)
        # print(f"padding shape: {padding.size()}")
        # labels = torch.cat((labels.float(), padding), dim=2)

        labels = self.txt_encoder(labels)

        # if len(labels.size()) > 2:
        di = 1
        if len(labels.size()) == 1:
            di = 0

        labels = labels.view(-1, 1, 1, labels.size(di))

        # print(f"labels shape: {labels.size()}")
        # print(f"out_code_weight shape: {out_code_weight.size()}")
        # print(f"out_code_bias shape: {out_code_bias.size()}")
        # print(f"out_code shape: {out_code.size()}")

        # return labels

        return labels * out_code_weight + out_code_bias

##############################################################################
# Losses
##############################################################################


class GANLoss(nn.Module):
    def __init__(self, use_lsgan=True, target_real_label=1.0, target_fake_label=0.0,
                 tensor=torch.FloatTensor):
        super(GANLoss, self).__init__()
        self.real_label = target_real_label
        self.fake_label = target_fake_label
        self.real_label_var = None
        self.fake_label_var = None
        self.Tensor = tensor
        if use_lsgan:
            self.loss = nn.MSELoss()
        else:
            self.loss = nn.BCELoss()

    def get_target_tensor(self, input, target_is_real):
        target_tensor = None
        if target_is_real:
            create_label = ((self.real_label_var is None) or
                            (self.real_label_var.numel() != input.numel()))
            if create_label:
                real_tensor = self.Tensor(input.size()).fill_(self.real_label)
                self.real_label_var = Variable(
                    real_tensor, requires_grad=False)
            target_tensor = self.real_label_var
        else:
            create_label = ((self.fake_label_var is None) or
                            (self.fake_label_var.numel() != input.numel()))
            if create_label:
                fake_tensor = self.Tensor(input.size()).fill_(self.fake_label)
                self.fake_label_var = Variable(
                    fake_tensor, requires_grad=False)
            target_tensor = self.fake_label_var
        return target_tensor

    def __call__(self, input, target_is_real):
        if isinstance(input[0], list):
            loss = 0
            for input_i in input:
                pred = input_i[-1]
                target_tensor = self.get_target_tensor(pred, target_is_real)
                loss += self.loss(pred, target_tensor)
            return loss
        else:
            target_tensor = self.get_target_tensor(input[-1], target_is_real)
            return self.loss(input[-1], target_tensor)


class VGGLoss(nn.Module):
    def __init__(self, gpu_ids):
        super(VGGLoss, self).__init__()
        self.vgg = Vgg19().cuda()
        self.criterion = nn.L1Loss()
        self.weights = [1.0/32, 1.0/16, 1.0/8, 1.0/4, 1.0]

    def forward(self, x, y):
        x_vgg, y_vgg = self.vgg(x), self.vgg(y)
        loss = 0
        for i in range(len(x_vgg)):
            loss += self.weights[i] * \
                self.criterion(x_vgg[i], y_vgg[i].detach())
        return loss

##############################################################################
# Generator
##############################################################################


class LocalEnhancer(nn.Module):
    def __init__(self, input_nc, output_nc, ngf=32, n_downsample_global=3, n_blocks_global=9,
                 n_local_enhancers=1, n_blocks_local=3, norm_layer=nn.BatchNorm2d, padding_type='reflect', cond=False,
                 n_self_attention=0, acm_dim=32, img_size=512, vocab_size=512):
        super(LocalEnhancer, self).__init__()
        self.n_local_enhancers = n_local_enhancers
        self.cond = cond

        ###### global generator model #####
        # GlobalGenerator must be trained at twice the ngf for load_pretrain to work
        ngf_global = ngf * (2**n_local_enhancers)

        print("***********")
        print(f"making global ngf: {ngf_global}")
        print("***********")

        global_img_size = img_size // (2 * n_local_enhancers)

        print("***********")
        print(f"global image size: {global_img_size}")
        print("***********")

        model_global = GlobalGenerator(
            input_nc, output_nc, ngf_global,
            n_downsample_global, n_blocks_global,
            norm_layer,
            n_self_attention=n_self_attention,
            cond=self.cond,
            img_size=global_img_size,
            vocab_size=vocab_size).model
        # get rid of final convolution layers
        model_global = [model_global[i] for i in range(len(model_global)-3)]

        if self.cond:
            self.model = MultiSequential(*model_global)
        else:
            self.model = nn.Sequential(*model_global)

        ###### local enhancer layers #####
        for n in range(1, n_local_enhancers+1):
            # downsample
            ngf_global = ngf * (2**(n_local_enhancers-n))
            global_img_size = img_size // (2 ** (n_local_enhancers-n))

            if self.cond:
                print(f"acm{n} image size: {global_img_size}")
                acm = ACM(acm_dim, img_size=global_img_size,
                          vocab_size=vocab_size)
                setattr(self, 'acm'+str(n), acm)

            model_downsample = [nn.ReflectionPad2d(3), nn.Conv2d(input_nc, ngf_global, kernel_size=7, padding=0),
                                norm_layer(ngf_global), nn.ReLU(True),
                                nn.Conv2d(ngf_global, ngf_global * 2,
                                          kernel_size=3, stride=2, padding=1),
                                norm_layer(ngf_global * 2), nn.ReLU(True)]
            # residual blocks
            model_upsample = []
            for i in range(n_blocks_local):
                model_upsample += [ResnetBlock(ngf_global * 2,
                                               padding_type=padding_type, norm_layer=norm_layer)]

            # upsample
            model_upsample += [nn.ConvTranspose2d(ngf_global * 2, ngf_global, kernel_size=3, stride=2, padding=1, output_padding=1),
                               norm_layer(ngf_global), nn.ReLU(True)]

            # final convolution
            if n == n_local_enhancers:
                model_upsample += [nn.ReflectionPad2d(3), nn.Conv2d(
                    ngf, output_nc, kernel_size=7, padding=0), nn.Tanh()]

            setattr(self, 'model'+str(n)+'_1',
                    nn.Sequential(*model_downsample))
            setattr(self, 'model'+str(n)+'_2', nn.Sequential(*model_upsample))

        self.downsample = nn.AvgPool2d(
            3, stride=2, padding=[1, 1], count_include_pad=False)

    def forward(self, *input_orig):

        if self.cond:
            labels, input = input_orig
            # input = self.acm(*input_orig)
        else:
            input = input_orig
            if isinstance(input, tuple):
                input = input[0]

        # create input pyramid
        input_downsampled = [input]
        for i in range(self.n_local_enhancers):
            # print(input_downsampled[-1])
            input_downsampled.append(self.downsample(input_downsampled[-1]))

        # output at coarest level
        if self.cond:
            output_prev = self.model(labels, input_downsampled[-1])
        else:
            output_prev = self.model(input_downsampled[-1])
        # build up one layer at a time
        for n_local_enhancers in range(1, self.n_local_enhancers+1):
            model_downsample = getattr(
                self, 'model'+str(n_local_enhancers)+'_1')
            model_upsample = getattr(self, 'model'+str(n_local_enhancers)+'_2')
            input_i = input_downsampled[self.n_local_enhancers -
                                        n_local_enhancers]

            if self.cond:
                acm = getattr(self, 'acm' + str(n_local_enhancers))
                output_prev = model_upsample(
                    model_downsample(acm(labels, input_i)) + output_prev)
            else:
                ds = model_downsample(input_i)
                output_prev = model_upsample(ds + output_prev)
        return output_prev


class MultiSequential(nn.Sequential):
    def forward(self, *inputs):
        for module in self._modules.values():
            if type(inputs) == tuple:
                inputs = module(*inputs)
            else:
                inputs = module(inputs)
        return inputs


class GlobalGenerator(nn.Module):
    def __init__(self, input_nc, output_nc, ngf=64, n_downsampling=3, n_blocks=9, norm_layer=nn.BatchNorm2d,
                 padding_type='reflect', cond=False, n_self_attention=0, acm_dim=64, img_size=512, vocab_size=512):
        assert(n_blocks >= 0)
        super(GlobalGenerator, self).__init__()
        activation = nn.ReLU(True)

        activation = Mish()

        self.cond = cond

        model = []
        if self.cond:
            model = [ACM(acm_dim, img_size=img_size, vocab_size=vocab_size)]

        model += [nn.ReflectionPad2d(3), nn.Conv2d(input_nc, ngf,
                                                   kernel_size=7, padding=0), norm_layer(ngf), activation]
        # downsample
        for i in range(n_downsampling):
            mult = 2**i
            model += [SNConv2d(ngf * mult, ngf * mult * 2, kernel_size=3, stride=2, padding=1),
                      norm_layer(ngf * mult * 2), activation]

        # resnet blocks
        mult = 2**n_downsampling
        for i in range(n_blocks):
            model += [ResnetBlock(ngf * mult, padding_type=padding_type,
                                  activation=activation, norm_layer=norm_layer)]

        # self attention
        for i in range(n_self_attention):
            model += [SelfAttention2d(ngf * mult),
                      norm_layer(int(ngf * mult)), activation]

        # upsample
        for i in range(n_downsampling):
            mult = 2**(n_downsampling - i)
            model += [nn.ConvTranspose2d(ngf * mult, int(ngf * mult / 2), kernel_size=3, stride=2, padding=1, output_padding=1),
                      norm_layer(int(ngf * mult / 2)), activation]
        model += [nn.ReflectionPad2d(3), SNConv2d(ngf,
                                                  output_nc, kernel_size=7, padding=0), nn.Tanh()]
        if self.cond:
            self.model = MultiSequential(*model)
        else:
            self.model = nn.Sequential(*model)

    def forward(self, *input):
        return self.model(*input)


# Define a resnet block
class ResnetBlock(nn.Module):
    def __init__(self, dim, padding_type, norm_layer, activation=nn.ReLU(True), use_dropout=False):
        super(ResnetBlock, self).__init__()
        self.conv_block = self.build_conv_block(
            dim, padding_type, norm_layer, activation, use_dropout)

    def build_conv_block(self, dim, padding_type, norm_layer, activation, use_dropout):
        conv_block = []
        p = 0
        if padding_type == 'reflect':
            conv_block += [nn.ReflectionPad2d(1)]
        elif padding_type == 'replicate':
            conv_block += [nn.ReplicationPad2d(1)]
        elif padding_type == 'zero':
            p = 1
        else:
            raise NotImplementedError(
                'padding [%s] is not implemented' % padding_type)

        conv_block += [SNConv2d(dim, dim, kernel_size=3, padding=p),
                       norm_layer(dim),
                       activation]
        if use_dropout:
            conv_block += [nn.Dropout(0.5)]

        p = 0
        if padding_type == 'reflect':
            conv_block += [nn.ReflectionPad2d(1)]
        elif padding_type == 'replicate':
            conv_block += [nn.ReplicationPad2d(1)]
        elif padding_type == 'zero':
            p = 1
        else:
            raise NotImplementedError(
                'padding [%s] is not implemented' % padding_type)
        conv_block += [nn.Conv2d(dim, dim, kernel_size=3, padding=p),
                       norm_layer(dim)]

        return nn.Sequential(*conv_block)

    def forward(self, x):
        out = x + self.conv_block(x)
        return out


class Encoder(nn.Module):
    def __init__(self, input_nc, output_nc, ngf=32, n_downsampling=4, norm_layer=nn.BatchNorm2d, img_size=512):
        super(Encoder, self).__init__()
        self.output_nc = output_nc

        model = [ACM(ngf, img_size=img_size)]

        model += [nn.ReflectionPad2d(3), nn.Conv2d(input_nc, ngf, kernel_size=7, padding=0),
                  norm_layer(ngf), nn.ReLU(True)]
        # downsample
        for i in range(n_downsampling):
            mult = 2**i
            model += [nn.Conv2d(ngf * mult, ngf * mult * 2, kernel_size=3, stride=2, padding=1),
                      norm_layer(ngf * mult * 2), nn.ReLU(True)]

        # upsample
        for i in range(n_downsampling):
            mult = 2**(n_downsampling - i)
            model += [nn.ConvTranspose2d(ngf * mult, int(ngf * mult / 2),
                                         kernel_size=3,
                                         stride=2,
                                         padding=1,
                                         output_padding=1),
                      norm_layer(int(ngf * mult / 2)),
                      nn.ReLU(True)]

        model += [nn.ReflectionPad2d(3), nn.Conv2d(ngf,
                                                   output_nc, kernel_size=7, padding=0), nn.Tanh()]
        self.model = MultiSequential(*model)

    def forward(self, *input):
        return self.model(*input)


class MultiscaleDiscriminator(nn.Module):
    def __init__(self, input_nc, ndf=64, n_layers=3, norm_layer=nn.BatchNorm2d,
                 use_sigmoid=False, num_D=3, getIntermFeat=False, n_self_attention=1):
        super(MultiscaleDiscriminator, self).__init__()
        self.num_D = num_D
        self.n_layers = n_layers
        self.getIntermFeat = getIntermFeat

        for i in range(num_D):
            netD = NLayerDiscriminator(
                input_nc, ndf, n_layers, norm_layer, use_sigmoid, getIntermFeat, n_self_attention)
            if getIntermFeat:
                for j in range(n_layers+2):
                    setattr(self, 'scale'+str(i)+'_layer' +
                            str(j), getattr(netD, 'model'+str(j)))
            else:
                setattr(self, 'layer'+str(i), netD.model)

        self.downsample = nn.AvgPool2d(
            3, stride=2, padding=[1, 1], count_include_pad=False)

    def singleD_forward(self, model, input):
        if self.getIntermFeat:
            result = [input]
            for i in range(len(model)):
                result.append(model[i](result[-1]))
            return result[1:]
        else:
            return [model(input)]

    def forward(self, input):
        num_D = self.num_D
        result = []
        input_downsampled = input
        for i in range(num_D):
            if self.getIntermFeat:
                model = [getattr(self, 'scale'+str(num_D-1-i)+'_layer'+str(j))
                         for j in range(self.n_layers+2)]
            else:
                model = getattr(self, 'layer'+str(num_D-1-i))
            result.append(self.singleD_forward(model, input_downsampled))
            if i != (num_D-1):
                input_downsampled = self.downsample(input_downsampled)
        return result

# Defines the PatchGAN discriminator with the specified arguments.


class NLayerDiscriminator(nn.Module):
    def __init__(self, input_nc, ndf=64, n_layers=3, norm_layer=nn.BatchNorm2d, use_sigmoid=False, getIntermFeat=False,
                 n_self_attention=1):
        super(NLayerDiscriminator, self).__init__()
        self.getIntermFeat = getIntermFeat
        self.n_layers = n_layers
        self.n_self_attention = n_self_attention

        kw = 4
        padw = int(np.floor((kw-1.0)/2))
        sequence = [[SNConv2d(input_nc, ndf, kernel_size=kw,
                              stride=2, padding=padw), nn.LeakyReLU(0.2, True)]]

        nf = ndf
        for n in range(1, n_layers):
            nf_prev = nf
            nf = min(nf * 2, 512)
            sequence += [[
                SNConv2d(nf_prev, nf, kernel_size=kw, stride=2, padding=padw),
                norm_layer(nf), nn.LeakyReLU(0.2, True)
            ]]

        nf_prev = nf
        nf = min(nf * 2, 512)

        # TODO: use n_self_attention and increase number of self attention layers

        sequence += [[
            # SelfAttention2d(nf_prev),
            SNConv2d(nf_prev, nf, kernel_size=kw, stride=1, padding=padw),
            norm_layer(nf),
            nn.LeakyReLU(0.2, True)
        ]]

        sequence += [[SNConv2d(nf, 1, kernel_size=kw,
                               stride=1, padding=padw)]]

        if use_sigmoid:
            sequence += [[nn.Sigmoid()]]

        if getIntermFeat:
            for n in range(len(sequence)):
                setattr(self, 'model'+str(n), nn.Sequential(*sequence[n]))
        else:
            sequence_stream = []
            for n in range(len(sequence)):
                sequence_stream += sequence[n]
            self.model = nn.Sequential(*sequence_stream)

    def forward(self, input):
        if self.getIntermFeat:
            res = [input]
            for n in range(self.n_layers+2):
                model = getattr(self, 'model'+str(n))
                res.append(model(res[-1]))
            return res[1:]
        else:
            return self.model(input)


class Vgg19(torch.nn.Module):
    def __init__(self, requires_grad=False):
        super(Vgg19, self).__init__()
        vgg_pretrained_features = models.vgg19(pretrained=True).features
        self.slice1 = torch.nn.Sequential()
        self.slice2 = torch.nn.Sequential()
        self.slice3 = torch.nn.Sequential()
        self.slice4 = torch.nn.Sequential()
        self.slice5 = torch.nn.Sequential()
        for x in range(2):
            self.slice1.add_module(str(x), vgg_pretrained_features[x])
        for x in range(2, 7):
            self.slice2.add_module(str(x), vgg_pretrained_features[x])
        for x in range(7, 12):
            self.slice3.add_module(str(x), vgg_pretrained_features[x])
        for x in range(12, 21):
            self.slice4.add_module(str(x), vgg_pretrained_features[x])
        for x in range(21, 30):
            self.slice5.add_module(str(x), vgg_pretrained_features[x])
        if not requires_grad:
            for param in self.parameters():
                param.requires_grad = False

    def forward(self, X):
        h_relu1 = self.slice1(X)
        h_relu2 = self.slice2(h_relu1)
        h_relu3 = self.slice3(h_relu2)
        h_relu4 = self.slice4(h_relu3)
        h_relu5 = self.slice5(h_relu4)
        out = [h_relu1, h_relu2, h_relu3, h_relu4, h_relu5]
        return out
