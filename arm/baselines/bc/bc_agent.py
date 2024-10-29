import copy
import logging
import os
from typing import List

import torch
import torch.nn as nn
import torch.nn.functional as F
from yarr.agents.agent import Agent, Summary, ActResult, \
    ScalarSummary, HistogramSummary

from arm import utils
from arm.utils import stack_on_channel

NAME = 'BCAgent'
REPLAY_ALPHA = 0.7
REPLAY_BETA = 1.0


class Actor(nn.Module):

    def __init__(self, actor_network: nn.Module):
        super(Actor, self).__init__()
        self._actor_network = copy.deepcopy(actor_network)
        self._actor_network.build()

    def forward(self, observations, robot_state):
        mu = self._actor_network(observations, robot_state)
        return mu


class BCAgent(Agent):

    def __init__(self,
                 actor_network: nn.Module,
                 camera_name: str,
                 lr: float = 0.01,
                 weight_decay: float = 1e-5,
                 grad_clip: float = 20.0):
        self._camera_name = camera_name
        self._actor_network = actor_network
        self._lr = lr
        self._weight_decay = weight_decay
        self._grad_clip = grad_clip

    def build(self, training: bool, device: torch.device = None):
        if device is None:
            device = torch.device('cpu')
        self._actor = Actor(self._actor_network).to(device).train(training)
        if training:
            self._actor_optimizer = torch.optim.Adam(
                self._actor.parameters(), lr=self._lr,
                weight_decay=self._weight_decay)
            logging.info('# Actor Params: %d' % sum(
                p.numel() for p in self._actor.parameters() if p.requires_grad))
        else:
            for p in self._actor.parameters():
                p.requires_grad = False
        self._device = device

    def _grad_step(self, loss, opt, model_params=None, clip=None):
        opt.zero_grad()
        loss.backward()
        if clip is not None and model_params is not None:
            nn.utils.clip_grad_value_(model_params, clip)
        opt.step()

    def update(self, step: int, replay_sample: dict) -> dict:
        robot_state = stack_on_channel(replay_sample['low_dim_state'][:, -1:])
        observations = [
            stack_on_channel(replay_sample['%s_rgb' % self._camera_name]),
            stack_on_channel(replay_sample['%s_point_cloud' % self._camera_name])
        ]
        mu = self._actor(observations, robot_state)
        loss_weights = utils.loss_weights(replay_sample, REPLAY_BETA)
        delta = F.mse_loss(
            mu, replay_sample['action'], reduction='none').mean(1)
        loss = (delta * loss_weights).mean()
        self._grad_step(loss, self._actor_optimizer,
                        self._actor.parameters(), self._grad_clip)
        self._summaries = {
            'pi/loss': loss,
            'pi/mu': mu.mean(),
        }
        return {'priority': delta ** REPLAY_ALPHA}

    def _normalize_quat(self, x):
        return x / x.square().sum(dim=1).sqrt().unsqueeze(-1)

    def act(self, step: int, observation: dict,
            deterministic=False) -> ActResult:
        with torch.no_grad():
            observations = [
                stack_on_channel(observation['%s_rgb' % self._camera_name]),
                stack_on_channel(observation['%s_point_cloud' % self._camera_name])
            ]
            robot_state = stack_on_channel(observation['low_dim_state'][:, -1:])
            mu = self._actor(observations, robot_state)
            mu = torch.cat(
                [mu[:, :3], self._normalize_quat(mu[:, 3:7]), mu[:, 7:]], dim=-1)
            return ActResult(mu[0])

    def update_summaries(self) -> List[Summary]:
        summaries = []
        for n, v in self._summaries.items():
            summaries.append(ScalarSummary('%s/%s' % (NAME, n), v))

        for tag, param in self._actor.named_parameters():
            summaries.append(
                HistogramSummary('%s/gradient/%s' % (NAME, tag), param.grad))
            summaries.append(
                HistogramSummary('%s/weight/%s' % (NAME, tag), param.data))

        return summaries

    def act_summaries(self) -> List[Summary]:
        return []

    def load_weights(self, savedir: str):
        self._actor.load_state_dict(
            torch.load(os.path.join(savedir, 'bc_actor.pt'),
                       map_location=torch.device('cpu')))

    def save_weights(self, savedir: str):
        torch.save(self._actor.state_dict(),
                   os.path.join(savedir, 'bc_actor.pt'))
