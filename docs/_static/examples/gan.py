import os

import numpy as np
import torch
import torch.nn as nn
import torchvision.transforms as transforms
from torch.autograd import Variable
from torch.utils.data import DataLoader
from torchvision import datasets
from torchvision.utils import save_image

import torchbearer as tb
import torchbearer.callbacks as callbacks
from torchbearer import state_key

os.makedirs('images', exist_ok=True)

# Define constants
epochs = 200
batch_size = 64
lr = 0.0002
nworkers = 8
latent_dim = 100
sample_interval = 400
img_shape = (1, 28, 28)
adversarial_loss = torch.nn.BCELoss()
device = 'cuda'
valid = torch.ones(batch_size, 1, device=device)
fake = torch.zeros(batch_size, 1, device=device)

# Register state keys (optional)
GEN_IMGS = state_key('gen_imgs')
DISC_GEN = state_key('disc_gen')
DISC_GEN_DET = state_key('disc_gen_det')
DISC_REAL = state_key('disc_real')
G_LOSS = state_key('g_loss')
D_LOSS = state_key('d_loss')

DISC_OPT = state_key('disc_opt')
DISC_MODEL = state_key('disc_model')
DISC_IMGS = state_key('disc_imgs')
DISC_CRIT = state_key('disc_crit')



class Generator(nn.Module):
    def __init__(self):
        super(Generator, self).__init__()

        def block(in_feat, out_feat, normalize=True):
            layers = [nn.Linear(in_feat, out_feat)]
            if normalize:
                layers.append(nn.BatchNorm1d(out_feat, 0.8))
            layers.append(nn.LeakyReLU(0.2, inplace=True))
            return layers

        self.model = nn.Sequential(
            *block(latent_dim, 128, normalize=False),
            *block(128, 256),
            *block(256, 512),
            *block(512, 1024),
            nn.Linear(1024, int(np.prod(img_shape))),
            nn.Tanh()
        )

    def forward(self, real_imgs, state):
        z = Variable(torch.Tensor(np.random.normal(0, 1, (real_imgs.shape[0], latent_dim)))).to(state[tb.DEVICE])
        img = self.model(z)
        img = img.view(img.size(0), *img_shape)
        return img


class Discriminator(nn.Module):
    def __init__(self):
        super(Discriminator, self).__init__()

        self.model = nn.Sequential(
            nn.Linear(int(np.prod(img_shape)), 512),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(512, 256),
            nn.LeakyReLU(0.2, inplace=True),
            nn.Linear(256, 1),
            nn.Sigmoid()
        )

    def forward(self, img, state):
        img_flat = img.view(img.size(0), -1)
        validity = self.model(img_flat)

        return validity

def gen_crit(state):
    loss =  adversarial_loss(state[DISC_MODEL](state[tb.Y_PRED], state), valid)
    state[G_LOSS] = loss
    return loss

def disc_crit(state):
    real_loss = adversarial_loss(state[DISC_MODEL](state[tb.X], state), valid)
    fake_loss = adversarial_loss(state[DISC_MODEL](state[tb.Y_PRED].detach(), state), fake)
    loss = (real_loss + fake_loss) / 2
    state[D_LOSS] = loss
    return loss

batch = torch.randn(25, latent_dim).to(device)
@callbacks.on_step_training
def saver_callback(state):
    batches_done = state[tb.EPOCH] * len(state[tb.GENERATOR]) + state[tb.BATCH]
    if batches_done % sample_interval == 0:
        samples = state[tb.MODEL](batch, state)
        save_image(samples, 'images/%d.png' % batches_done, nrow=5, normalize=True)


# Configure data loader
os.makedirs('./data/mnist', exist_ok=True)
transform = transforms.Compose([
                        transforms.ToTensor(),
                        transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5))
                   ])

dataset = datasets.MNIST('./data/mnist', train=True, download=True, transform=transform)

dataloader = torch.utils.data.DataLoader(dataset, batch_size=batch_size, shuffle=True, drop_last=True)


# Model and optimizer
generator = Generator()
discriminator = Discriminator()
optimizer_G = torch.optim.Adam(generator.parameters(), lr=lr, betas=(0.5, 0.999))
optimizer_D = torch.optim.Adam(discriminator.parameters(), lr=lr, betas=(0.5, 0.999))


@tb.metrics.running_mean
@tb.metrics.mean
class g_loss(tb.metrics.Metric):
    def __init__(self):
        super().__init__('g_loss')

    def process(self, state):
        return state[G_LOSS]


@tb.metrics.running_mean
@tb.metrics.mean
class d_loss(tb.metrics.Metric):
    def __init__(self):
        super().__init__('d_loss')

    def process(self, state):
        return state[D_LOSS]

def closure_gen(self, state):
    # Zero grads
    state[tb.OPTIMIZER].zero_grad()

    # Forward Pass
    if self.pass_state:
        state[tb.Y_PRED] = state[tb.MODEL](state[tb.X], state=state)
    else:
        state[tb.Y_PRED] = state[tb.MODEL](state[tb.X])

    state[tb.CALLBACK_LIST].on_forward(state)

    # Loss Calculation
    state[tb.LOSS] = state[tb.CRITERION](state)

    state[tb.CALLBACK_LIST].on_criterion(state)

    # Backwards pass
    state[tb.LOSS].backward(**state[tb.BACKWARD_ARGS])

    state[tb.CALLBACK_LIST].on_backward(state)

def closure_disc(self, state):
    # Zero grads
    state[DISC_OPT].zero_grad()

    # Forward Pass
    if self.pass_state:
        state[DISC_IMGS] = state[DISC_MODEL](state[tb.Y_PRED], state=state)
    else:
        state[DISC_IMGS] = state[DISC_MODEL](state[tb.Y_PRED])

    state[tb.CALLBACK_LIST].on_forward(state)

    # Loss Calculation
    state[tb.LOSS] = state[DISC_CRIT](state)

    state[tb.CALLBACK_LIST].on_criterion(state)

    # Backwards pass
    state[tb.LOSS].backward(**state[tb.BACKWARD_ARGS])

    state[tb.CALLBACK_LIST].on_backward(state)
    state[DISC_OPT].step()

def closure(self, state):
    closure_gen(self, state)
    closure_disc(self, state)


trial = tb.Trial(generator, optimizer_G, criterion=gen_crit, metrics=['loss', g_loss(), d_loss()],
                            callbacks=[saver_callback], pass_state=True)
trial.with_train_generator(dataloader)
trial.state[DISC_MODEL] = discriminator.cuda()
trial.state[DISC_OPT] = optimizer_D
trial.state[DISC_CRIT] = disc_crit
trial.with_closure(closure)
trial.to(device)
trial.run(epochs=200)
