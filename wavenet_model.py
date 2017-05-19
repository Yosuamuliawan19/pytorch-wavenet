import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
from scipy.io import wavfile
from random import randint
from wavenet_modules import *


class WaveNetModel(nn.Module):
    def __init__(self,
                 layers,
                 blocks,
                 dilation_channels,
                 residual_channels,
                 skip_channels,
                 classes,
                 kernel_size=2,
                 dtype=torch.FloatTensor):

        super(WaveNetModel, self).__init__()

        self.layers = layers
        self.blocks = blocks
        self.dilation_channels = dilation_channels
        self.residual_channels = residual_channels
        self.skip_channels = skip_channels
        self.classes = classes
        self.kernel_size = kernel_size
        self.dtype = dtype

        # build model
        receptive_field = 0
        init_dilation = 1

        self.dilations = []
        self.dilated_queues = []
        self.dilated_convs = nn.ModuleList()
        self.residual_convs = nn.ModuleList()
        self.skip_convs = nn.ModuleList()

        # 1x1 convolution to create channels
        self.start_conv = nn.Conv1d(in_channels=1,
                                    out_channels=residual_channels,
                                    kernel_size=1,
                                    bias=False)

        for b in range(blocks):
            additional_scope = kernel_size - 1
            new_dilation = 1
            for i in range(layers):
                # dilations of this layer
                self.dilations.append((new_dilation, init_dilation))

                # dilated queues for fast generation
                self.dilated_queues.append(DilatedQueue(max_length=(kernel_size - 1) * new_dilation + 1,
                                                        num_channels=residual_channels,
                                                        dilation=new_dilation,
                                                        dtype=dtype))

                # dilated convolution
                self.dilated_convs.append(nn.Conv1d(in_channels=residual_channels,
                                                    out_channels=dilation_channels*2,
                                                    kernel_size=kernel_size,
                                                    bias=False))

                # 1x1 convolution for residual connection
                self.residual_convs.append(nn.Conv1d(in_channels=dilation_channels,
                                                     out_channels=residual_channels,
                                                     kernel_size=1,
                                                     bias=False))

                # 1x1 convolution for skip connection
                self.skip_convs.append(nn.Conv1d(in_channels=dilation_channels,
                                                 out_channels=skip_channels,
                                                 kernel_size=1,
                                                 bias=False))

                receptive_field += additional_scope
                additional_scope *= 2
                init_dilation = new_dilation
                new_dilation *= 2

        self.end_conv = nn.Conv1d(in_channels=skip_channels,
                                  out_channels=classes,
                                  kernel_size=1,
                                  bias=True)

        self.output_length = 2 ** (layers - 1)
        self.receptive_field = receptive_field + self.output_length

    def wavenet(self, input, dilation_func):

        x = self.start_conv(input)
        skip = 0

        # WaveNet layers
        for i in range(self.blocks * self.layers):

            #			 |---------------------------------|		*residual*
            #            |                                 |
            # 			 |	   |-- tanh --|			       |
            # -> dilate -|-- conv         * ----|-- 1x1 -- + -->	*input*
            #				   |-- sigm --|     |
            #							       1x1
            #							        |
            # --------------------------------> + ------------->	*skip*

            (dilation, init_dilation) = self.dilations[i]

            residual = dilation_func(x, dilation, init_dilation, i)

            # dilated convolution
            x = self.dilated_convs[i](residual)
            filter = x[:, 0:self.dilation_channels, :]
            filter = F.tanh(filter)
            gate = x[:, self.dilation_channels:, :]
            gate = F.sigmoid(gate)
            x = filter * gate

            # parametrized skip connection
            s = x
            if x.size(2) != 1:
                s = dilate(x, 1, init_dilation=dilation)
            s = self.skip_convs[i](s)
            if skip != 0:
                skip = skip[:, :, -s.size(2):]
            skip = s + skip

            x = self.residual_convs[i](x)
            x = x + residual[:, :, (self.kernel_size - 1):]

        x = F.relu(skip)
        x = self.end_conv(x)

        return x

    def wavenet_dilate(self, input, dilation, init_dilation, i):
        x = dilate(input, dilation, init_dilation)
        return x

    def queue_dilate(self, input, dilation, init_dilation, i):
        queue = self.dilated_queues[i]
        queue.enqueue(input.data[0])
        x = queue.dequeue(num_deq=self.kernel_size,
                          dilation=dilation)
        x = x.unsqueeze(0)

        return x

    def forward(self, input):
        x = self.wavenet(input,
                         dilation_func=self.wavenet_dilate)

        # reshape output
        [n, c, l] = x.size()
        l = self.output_length
        x = x[:, :, -l:]
        x = x.transpose(1, 2).contiguous()
        x = x.view(n * l, c)

        return x

    def generate_fast(self,
                      num_samples,
                      first_samples=None,
                      sampled_generation=False,
                      temperature=1.,
                      progress_callback=None):

        self.eval()
        if first_samples is None:
            first_samples = self.dtype(1).zero_()

        # reset queues
        for queue in self.dilated_queues:
            queue.reset()

        num_given_samples = first_samples.size(0)
        total_samples = num_given_samples + num_samples
        progress_dist = total_samples // 100

        input = Variable(first_samples[0:1], volatile=True).view(1, 1, 1)

        # fill queues with given samples
        for i in range(num_given_samples - 1):
            x = self.wavenet(input,
                             dilation_func=self.queue_dilate)
            input = Variable(first_samples[i + 1:i + 2], volatile=True).view(1, 1, 1)

            # progress feedback
            if i % progress_dist == 0:
                if progress_callback != None:
                    progress_callback(i, total_samples)

        # generate new samples
        generated = np.array([])
        for i in range(num_samples):
            x = self.wavenet(input,
                             dilation_func=self.queue_dilate)

            if sampled_generation:
                # sample from softmax distribution
                x = x.squeeze() / temperature
                prob = F.softmax(x)
                np_prob = prob.data.numpy()
                x = np.random.choice(self.classes, p=np_prob)
                x = np.array([x])
            else:
                # convert to sample value
                x = x.squeeze()
                x = torch.max(x, 0)[1][0]
                x = x.data.numpy()

            x = (x / self.classes) * 2. - 1
            x = mu_law_expansion(x, self.classes)

            generated = np.append(generated, x)

            # set new input
            input = Variable(self.dtype([[x]]), volatile=True)

            # progress feedback
            if (i + num_given_samples) % progress_dist == 0:
                if progress_callback != None:
                    progress_callback(i + num_given_samples, total_samples)

        return generated

    def parameter_count(self):
        par = list(self.parameters())
        s = sum([np.prod(list(d.size())) for d in par])
        return s