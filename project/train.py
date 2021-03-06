import os
import copy
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import torch.optim.lr_scheduler as LS
from torchvision import transforms
from torchvision.models import vgg
from tensorboardX import SummaryWriter
from PIL import Image

from models import Encoder, Decoder, Binarizer


class PerceptualLossNet(nn.Module):
    def __init__(self):
        super(PerceptualLossNet, self).__init__()
        self.vgg = vgg.vgg16(pretrained=True).features
        self.layer_map = {
            "3":"relu1_2",
            "8":"relu2_2",
            "15":"relu3_3",
            "22":"relu4_3"
        }
        for param in self.vgg.parameters():
            param.requires_grad = False
    def forward(self, x):
        out = {}
        for name, module in self.vgg._modules.items():
            x = module(x)
            if name in self.layer_map:
                out[self.layer_map[name]] = x
        return out


def percep_loss(perceptualLossNet, y, y_hat):
    a_features = perceptualLossNet(y)
    b_features = perceptualLossNet(y_hat)
    loss_perceptual = 0.
    for name in a_features:
        loss_perceptual += F.mse_loss(a_features[name], b_features[name])
    return loss_perceptual

def img_normalize(imgs):
    return (imgs+1.0)/2


def save_models(args, encoder, binarizer, decoder):
    torch.save(encoder.state_dict,
               'save/{}_encoder.pth'.format(args.model_name))
    torch.save(binarizer.state_dict,
               'save/{}_binarizer.pth'.format(args.model_name))
    torch.save(decoder.state_dict,
               'save/{}_decoder.pth'.format(args.model_name))


def train(train_params, args, train_loader, val_loader):
    encoder = Encoder().to(args.device)
    binarizer = Binarizer().to(args.device)
    decoder = Decoder().to(args.device)

    optimizer = optim.Adam(
        [{'params': encoder.parameters()},
         {'params': binarizer.parameters()},
         {'params': decoder.parameters()},
        ],
        lr=args.lr)

    scheduler = LS.MultiStepLR(optimizer, milestones=[3, 10, 20, 50, 100], gamma=0.5)

    l1loss = torch.nn.L1Loss()

    perceptualLossNet = PerceptualLossNet().to(args.device)

    best_loss = float('inf')
    best_encoder, best_binarizer, best_decoder = None, None, None
    full_patience = 10
    patience = full_patience
    batch_size = train_params['batch_size']
    writer = SummaryWriter('log/{}'.format(args.model_name))
    log_interval = int(len(train_loader) * 0.05)
    val_interval = len(train_loader)
    print('log_interval:', log_interval, 'val_interval:', val_interval)

    for epoch in range(train_params['epochs']):
        if epoch > 1:
            scheduler.step()
        print('== Epoch:', epoch)
        epoch_loss = 0
        for batch_idx, (sample_x, sample_y) in enumerate(train_loader):
            #print(f"batch_idx: {batch_idx}")
            sample_x = sample_x.to(args.device)
            sample_y = sample_y.to(args.device)

            encoder_h1 = (
                torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device),
                torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device)
            )
            encoder_h2 = (
                torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device),
                torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device))
            encoder_h3 = (
                torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device),
                torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device)
            )

            decoder_h1 = (
                torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device),
                torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device)
            )
            decoder_h2 = (
                torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device),
                torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device)
            )
            decoder_h3 = (
                torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device),
                torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device)
            )
            decoder_h4 = (
                torch.zeros(sample_x.size(0), 128, 128, 128).to(args.device),
                torch.zeros(sample_x.size(0), 128, 128, 128).to(args.device)
            )

            losses = []
            #losses = 0
            optimizer.zero_grad()

            residual = sample_x
            # residual = sample_x - 0.5
            for i in range(train_params['iterations']):
                # print('input:', residual.shape)
                x, encoder_h1, encoder_h2, encoder_h3 = encoder(
                    residual, encoder_h1, encoder_h2, encoder_h3)
                x = binarizer(x)
                # nbytes = x.detach().numpy().astype(np.bool).nbytes
                # print('\ncompressed:', x.shape, n_bytes)
                # print()
                output, decoder_h1, decoder_h2, decoder_h3, decoder_h4 = decoder(
                    x, decoder_h1, decoder_h2, decoder_h3, decoder_h4)
                # print('output:', output.shape)

                residual = sample_x - output
                loss_per_iter = residual.abs().mean()
                losses.append(loss_per_iter)

            loss = sum(losses) / train_params['iterations']

            if args.percep_weight != 0:
                loss = args.percep_weight * percep_loss(perceptualLossNet, residual, output) + \
                    (1 - args.percep_weight) * loss

            loss.backward()
            optimizer.step()
            epoch_loss += loss.item()

            if batch_idx % log_interval == 0:
                idx = epoch * int(len(train_loader.dataset) / batch_size) + batch_idx
                writer.add_scalar('loss', loss.item(), idx)
                writer.add_image('input_img', sample_x[0], idx)
                writer.add_image('recon_img', img_normalize(output[0]), idx)

            #if batch_idx % val_interval == 0 and batch_idx != 0:
            if batch_idx % val_interval == 0 and train_params['validate']:
                val_loss = 0
                for batch_idx, (sample_x, sample_y) in enumerate(val_loader):
                    sample_x = sample_x.to(args.device)
                    sample_y = sample_y.to(args.device)

                    encoder_h1 = (
                        torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device),
                        torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device)
                    )
                    encoder_h2 = (
                        torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device),
                        torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device))
                    encoder_h3 = (
                        torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device),
                        torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device)
                    )

                    decoder_h1 = (
                        torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device),
                        torch.zeros(sample_x.size(0), 512, 16, 16).to(args.device)
                    )
                    decoder_h2 = (
                        torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device),
                        torch.zeros(sample_x.size(0), 512, 32, 32).to(args.device)
                    )
                    decoder_h3 = (
                        torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device),
                        torch.zeros(sample_x.size(0), 256, 64, 64).to(args.device)
                    )
                    decoder_h4 = (
                        torch.zeros(sample_x.size(0), 128, 128, 128).to(args.device),
                        torch.zeros(sample_x.size(0), 128, 128, 128).to(args.device)
                    )
                    x, encoder_h1, encoder_h2, encoder_h3 = encoder(
                        sample_x, encoder_h1, encoder_h2, encoder_h3)
                    x = binarizer(x)
                    output, decoder_h1, decoder_h2, decoder_h3, decoder_h4 = decoder(
                        x, decoder_h1, decoder_h2, decoder_h3, decoder_h4)

                    val_loss += l1loss(output, sample_x).item()
                losses.append(loss_per_iter)
                writer.add_scalar('val_loss', val_loss / len(val_loader), idx)
                writer.flush()

                if best_loss > val_loss:
                    best_loss = val_loss
                    best_encoder = copy.deepcopy(encoder)
                    best_binarizer = copy.deepcopy(binarizer)
                    best_decoder = copy.deepcopy(decoder)
                    save_models(args, best_encoder, best_binarizer, best_decoder)
                    print('Improved: current best_loss on val:{}'.format(best_loss))
                    patience = full_patience
                else:
                    patience -= 1
                    print('patience', patience)
                    if patience == 0:
                        save_models(args, best_encoder, best_binarizer, best_decoder)
                        print('Early Stopped: Best L1 loss on val:{}'.format(best_loss))
                        writer.close()
                        return
        print(f"epoch loss: {epoch_loss}")

    print('Finished: Best L1 loss on val:{}'.format(best_loss))
    writer.close()
