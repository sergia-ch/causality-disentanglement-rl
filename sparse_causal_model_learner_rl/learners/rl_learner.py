import logging
import os
import traceback
from functools import partial
import ray

import gin
import gym
import numpy as np
import torch
from matplotlib import pyplot as plt
from tqdm import tqdm
from tqdm.auto import tqdm

from causal_util import load_env, WeightRestorer
from causal_util.collect_data import EnvDataCollector, compute_reward_to_go
from causal_util.helpers import one_hot_encode
from sparse_causal_model_learner_rl.learners.abstract_learner import AbstractLearner
from sparse_causal_model_learner_rl.visual.learner_visual import plot_model, graph_for_matrices, \
    select_threshold
from sparse_causal_model_learner_rl.visual.learner_visual import total_loss, loss_and_history, \
    plot_contour, plot_3d


@gin.configurable
class RLContext():
    """Collect data from an RL environment on a random policy."""
    def __init__(self, config):
        self.config = config
        self.env = self.create_env()
        self.collector = EnvDataCollector(self.env)
        self.vf_gamma = self.config.get('vf_gamma', 1.0)

        # Discrete action -> one-hot encoding
        if isinstance(self.env.action_space, gym.spaces.Discrete):
            self.to_onehot = True
            self.action_shape = (self.env.action_space.n,)
        else:
            self.to_onehot = False
            self.action_shape = self.env.action_space.shape

        self.additional_feature_keys = self.config.get('additional_feature_keys', [])


    def create_env(self):
        """Create the RL environment."""
        """Create an environment according to config."""
        if 'env_config_file' in self.config:
            gin.parse_config_file(self.config['env_config_file'])
        return load_env()

    def collect_steps(self, do_tqdm=False):
        # TODO: run a policy with curiosity reward instead of the random policy

        # removing old data
        self.collector.clear()

        # collecting data
        n_steps = self.config['env_steps']
        with tqdm(total=n_steps, disable=not do_tqdm) as pbar:
            while self.collector.steps < n_steps:
                done = False
                self.collector.reset()
                pbar.update(1)
                while not done:
                    _, _, done, _ = self.collector.step(self.collector.action_space.sample())
                    pbar.update(1)
        self.collector.flush()

    def get_context(self):
        # x: pre time-step, y: post time-step

        # observations, actions, rewards-to-go, total rewards
        obs_x, obs_y, obs, act_x, reward_to_go, episode_rewards = [], [], [], [], [], []
        done_y, rew_y = [], []

        for episode in self.collector.raw_data:
            rew = []
            is_multistep = len(episode) > 1
            for i, step in enumerate(episode):
                remaining_left = i
                remaining_right = len(episode) - 1 - i
                is_first = remaining_left == 0
                is_last = remaining_right == 0

                obs.append(step['observation'])

                if is_multistep and not is_first:
                    action = step['action']
                    if self.to_onehot:
                        action = one_hot_encode(self.action_shape[0], action)

                    rew_y.append(step['reward'])
                    done_y.append(1. * is_last)

                    obs_y.append(step['observation'])
                    act_x.append(action)
                    rew.append(step['reward'])

                if is_multistep and not is_last:
                    obs_x.append(step['observation'])

            rew_to_go_episode = compute_reward_to_go(rew, gamma=self.vf_gamma)
            episode_rewards.append(rew_to_go_episode[0])
            reward_to_go.extend(rew_to_go_episode)

        # for value function prediction
        assert len(reward_to_go) == len(obs_x)

        # for modelling
        assert len(obs_x) == len(act_x)

        # for reconstruction
        assert len(obs_x) == len(obs_y)

        obs_x = np.array(obs_x)
        obs_y = np.array(obs_y)
        obs = np.array(obs)
        act_x = np.array(act_x)
        reward_to_go = np.array(reward_to_go)
        done_y = np.array(done_y)
        rew_y = np.array(rew_y)
        episode_rewards = np.array(episode_rewards)

        context = {'obs_x': obs_x, 'obs_y': obs_y, 'action_x': act_x,
                   'rew_y': rew_y, 'done_y': done_y,
                   'obs': obs,
                   'reward_to_go': reward_to_go,
                   'episode_rewards': episode_rewards}

        return context



@ray.remote
class RemoteRLContext():
    """Collect data from a learner remotely."""
    def __init__(self, config, gin_config):
        gin.parse_config(gin_config)
        self.rl_context = RLContext(config)
    def collect_steps_and_context(self):
        self.rl_context.collect_steps()
        return self.rl_context.get_context()


@gin.configurable
class ExperienceReplayBuffer():
    """Collect data from RL contexts, and store it. Then, sample batches."""
    def __init__(self, buffer_limit_steps=1000000,
                 minibatch_size=5000,
                 shuffle_together=None):
        self.buffer_limit_steps = buffer_limit_steps
        self.minibatch_size = minibatch_size
        self.buffer = {}
        self.shuffle_together = shuffle_together

    def sample_batch(self, group_size_max=None):
        assert self.buffer, "Buffer is empty"

        if group_size_max is None:
            group_size_max = self.minibatch_size

        pre_context_return = {}
        left_keys = set(self.buffer.keys())
        for group in self.shuffle_together:
            left_keys.difference_update(group)
            group_lens = [len(self.buffer[key]) for key in group]
            group_len = group_lens[0]
            assert [group_len == l for l in group_lens], f"group lens must be the same {group} {group_lens}"

            if group_len > group_size_max:
                idxes_return = np.random.choice(
                    a=range(group_len), size=group_size_max, replace=False)
            else:
                idxes_return = range(group_len)

            for key in group:
                pre_context_return[key] = self.buffer[key][idxes_return]

        assert not left_keys, f"Some keys were not used: {left_keys} {self.shuffle_together}"
        return pre_context_return

    def limit_size(self):
        self.buffer = self.sample_batch(self.buffer_limit_steps)

    def observe(self, pre_context):
        for key in pre_context.keys():

            assert isinstance(pre_context[key], np.ndarray), f"Inputs must be numpy arrays {key} {type(pre_context[key])}"
            if key in self.buffer:
                self.buffer[key] = np.concatenate((self.buffer[key], pre_context[key]), axis=0)
            else:
                self.buffer[key] = pre_context[key]

        self.limit_size()


@gin.register
class CausalModelLearnerRL(AbstractLearner):
    """Learn a model for an RL environment with custom losses and parameters."""

    def __init__(self, *args, **kwargs):

        super().__init__(*args, **kwargs)

        # creating environment
        self.rl_context = RLContext(config=self.config)
        self.env = self.rl_context.env

        self.observation_shape = self.env.observation_space.shape
        self.action_shape = self.rl_context.action_shape

        self.feature_shape = self.config['feature_shape']
        if self.feature_shape is None:
            self.feature_shape = self.observation_shape

        self.additional_feature_keys = self.rl_context.additional_feature_keys
        self.additional_feature_shape = (len(self.additional_feature_keys),)

        self.model_kwargs = {'feature_shape': self.feature_shape,
                             'action_shape': self.action_shape,
                             'observation_shape': self.observation_shape,
                             'additional_feature_shape': self.additional_feature_shape}

        logging.info(self.model_kwargs)

        self.collect_remotely = self.config.get('collect_remotely', False)
        self.n_collectors = self.config.get('n_collectors', 1)
        if self.collect_remotely:
            self.remote_rl_contexts = [RemoteRLContext.remote(config=self.config,
                                                              gin_config=gin.config_str())
                                       for _ in range(self.n_collectors)]
            self.future_buffer_size = self.config.get('future_buffer_size', 10)
            self.next_context_refs = set()

        self.shuffle_together = [['obs_x', 'obs_y', 'action_x', 'reward_to_go', 'rew_y', 'done_y'],
                                 ['obs'], ['episode_rewards']]
        self.replay_buffer = ExperienceReplayBuffer(shuffle_together=self.shuffle_together)

    def collect_steps(self):
        raise NotImplementedError("Use collect_and_get_context")

    @property
    def _context_subclass(self):
        raise NotImplementedError("Use collect_and_get_context")

    def context_add_scalars(self, ctx):
        ctx['n_samples'] = len(ctx['obs_x'])
        ctx['additional_feature_keys'] = self.additional_feature_keys
        return ctx

    def collect_and_get_context(self):
        """Collect new data and return the training context."""

        if self.collect_remotely:
            # scheduling remote jobs...
            while len(self.next_context_refs) < self.future_buffer_size:
                remote_context_id = np.random.choice(range(len(self.remote_rl_contexts)))
                remote_context = self.remote_rl_contexts[remote_context_id]
                self.next_context_refs.add(remote_context.collect_steps_and_context.remote())

            ready_refs, non_ready_refs = ray.wait(list(self.next_context_refs), num_returns=1)
            ready_ref = ready_refs[0]
            self.next_context_refs.remove(ready_ref)
            pre_context = ray.get(ready_ref)
        else:
            self.rl_context.collect_steps()
            pre_context = self.rl_context.get_context()

        self.replay_buffer.observe(pre_context)
        pre_context_sample = self.replay_buffer.sample_batch()

        pre_context_sample = self.context_add_scalars(pre_context_sample)
        return self.wrap_context(pre_context_sample)

    @property
    def graph(self):
        """Return the current causal model."""
        return [self.model.Mf, self.model.Ma]

    def __repr__(self):
        return f"<RLLearner env={self.env} feature_shape={self.feature_shape} " \
               f"epochs={self.epochs} additional_feature_shape={self.additional_feature_shape}>"

    def visualize_loss_landscape(self, steps_skip=10, scale=5, n=20, mode='2d'):
        """Plot loss landscape in PCA space with the descent curve."""
        weight_names = [f"{t}/{param}" for t, model in self.trainables.items() for param, _ in
                        model.named_parameters()]

        self._last_loss_mode = mode

        results = {}

        # restore weights to original values
        with WeightRestorer(models=list(self.trainables.values())):
            for opt_label in self.config['optimizers'].keys():
                loss = partial(total_loss, learner=self, opt_label=opt_label)

                loss_w, flat_history = loss_and_history(self, loss, weight_names)
                flat_history = flat_history[::steps_skip]

                if mode == '2d':
                    res = plot_contour(flat_history, loss_w, n=n, scale=scale)
                elif mode == '3d':
                    res = plot_3d(flat_history, loss_w, n=n, scale=scale)
                else:
                    raise ValueError(f"Wrong mode: {mode}, needs to be 2d/3d.")
                results[opt_label] = res

        return results

    def visualize_model(self):
        return plot_model(self.model)

    def visualize_graph(self, threshold='auto', do_write=False):
        if threshold == 'auto':
            _ = select_threshold(self.model.Ma, do_plot=do_write, name='learner_action')
            _ = select_threshold(self.model.Mf, do_plot=do_write, name='learner_feature')
            threshold_act = select_threshold(self.model.Ma, do_plot=False, do_log=False,
                                             name='learner_action')
            threshold_f = select_threshold(self.model.Mf, do_plot=False, do_log=False,
                                           name='learner_feature')
            threshold = np.mean([threshold_act, threshold_f])
        ps, f_out = graph_for_matrices(self.model, threshold_act=threshold_act,
                                       threshold_f=threshold_f, do_write=do_write)
        return threshold, ps, f_out

    def maybe_write_artifacts(self, path_epoch, add_artifact_local):
        # writing figures if requested
        if self.epochs % self.config.get('graph_every', 5) == 0:
            os.makedirs(path_epoch, exist_ok=True)
            with path_epoch:
                try:
                    threshold, ps, f_out = self.visualize_graph(do_write=True)
                    artifact = path_epoch / (f_out + ".png")
                    add_artifact_local(artifact)
                except Exception as e:
                    logging.error(f"Error plotting causal graph: {self.epochs} {e} {type(e)}")
                    print(traceback.format_exc())

                try:
                    artifact = path_epoch / "threshold_learner_feature.png"
                    add_artifact_local(artifact)
                except Exception as e:
                    logging.error(
                        f"Error plotting threshold for feature: {self.epochs} {e} {type(e)}")
                    print(traceback.format_exc())

                try:
                    artifact = path_epoch / "threshold_learner_action.png"
                    add_artifact_local(artifact)
                except Exception as e:
                    logging.error(
                        f"Error plotting threshold for action: {self.epochs} {e} {type(e)}")
                    print(traceback.format_exc())

                try:
                    fig = self.visualize_model()
                    fig.savefig("model.png", bbox_inches="tight")
                    artifact = path_epoch / "model.png"
                    add_artifact_local(artifact)
                    plt.clf()
                    plt.close(fig)
                except Exception as e:
                    logging.error(f"Error plotting model: {self.epochs} {e} {type(e)}")
                    print(traceback.format_exc())

        if (self.epochs % self.config.get('loss_every', 100) == 0) and self.history:
            os.makedirs(path_epoch, exist_ok=True)
            with path_epoch:
                try:
                    for opt, (fig, ax) in self.visualize_loss_landscape().items():
                        if self._last_loss_mode == '2d':
                            fig.savefig(f"loss_{opt}.png", bbox_inches="tight")
                            artifact = path_epoch / f"loss_{opt}.png"
                            add_artifact_local(artifact)
                            plt.clf()
                            plt.close(fig)
                except Exception as e:
                    logging.error(f"Loss landscape error: {type(e)} {str(e)}")
                    print(traceback.format_exc())
