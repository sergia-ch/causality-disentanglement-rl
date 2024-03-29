import ray
from ray import tune
import logging

import gin
import gym
import numpy as np
from tqdm import tqdm
from tqdm.auto import tqdm
from time import sleep, time

from causal_util import load_env
from causal_util.collect_data import EnvDataCollector, compute_reward_to_go
from causal_util.helpers import one_hot_encode
from .rl_data_multi_step import get_multi_step_rl_context
from causal_util.helpers import one_hot_encode_vectorized
from sparse_causal_model_learner_rl.time_profiler.profiler import TimeProfiler


def get_shuffle_together(config):
    """Get which parameters need to be shuffled together, including multi-step values."""
    shuffle_together = config.get('shuffle_together', [])
    data_multistep = config.get('rl_multistep', [])
    for steps in data_multistep:
        group = []
        for s in range(1, steps + 1):
            group.append(f'multistep_{steps}_obs_{s}')
        for s in range(1, steps):
            group.append(f'multistep_{steps}_act_{s}')
            group.append(f'multistep_{steps}_rew_{s}')
            group.append(f'multistep_{steps}_done_{s}')
        shuffle_together.append(group)

    return shuffle_together


def ray_wait_all_non_blocking(futures):
    """Get all futures which are ready right now."""
    f_ready = []
    f_remaining = futures

    while f_remaining:
        f_ready_, f_remaining = ray.wait(f_remaining, num_returns=1, timeout=0)
        if not f_ready_:
            break
        else:
            f_ready.extend(f_ready_)
    return f_ready, f_remaining


class RLContext():
    """Collect data from an RL environment on a random policy."""
    def __init__(self, config, gin_config_str=None):
        if gin_config_str:
            with gin.unlock_config():
                gin.parse_config(gin_config_str)
        self.config = config
        self.env = self.create_env()
        self.collector = EnvDataCollector(self.env)
        self.vf_gamma = self.config.get('vf_gamma', 1.0)
        self.data_multistep = self.config.get('rl_multistep', [])
        self.profiler = None

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
            with gin.unlock_config():
                gin.parse_config_file(self.config['env_config_file'])
        return load_env()

    def sample_action(self, obs):
        # TODO: run a policy with curiosity reward instead of the random policy

        return self.collector.action_space.sample()

    def collect_steps(self, do_tqdm=False):

        # removing old data
        self.collector.clear()

        # collecting data
        n_steps = self.config['env_steps']
        with tqdm(total=n_steps, disable=not do_tqdm) as pbar:
            while self.collector.steps < n_steps:
                done = False
                obs = self.collector.reset()
                pbar.update(1)
                while not done:
                    obs, _, done, _ = self.collector.step(self.sample_action(obs))
                    pbar.update(1)
        self.collector.flush()

    def collect_get_context(self):
        self.profiler = TimeProfiler(enable=False, strict=False)
        profiler = self.profiler
        profiler.start('collect_steps')
        self.collect_steps()
        profiler.end('collect_steps')
        profiler.start('get_context')
        context = self.get_context()
        profiler.end('get_context')
        profiler.report()
        return context

    def get_context(self):
        # x: pre time-step, y: post time-step

        profiler = self.profiler or TimeProfiler(enable=False, strict=False)
        profiler.start('episodes')

        # observations, actions, rewards-to-go, total rewards
        obs_x, obs_y, obs, act_x, reward_to_go, episode_sum_rewards = [], [], [], [], [], []
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
            episode_sum_rewards.extend(list(np.cumsum(rew)))
            reward_to_go.extend(rew_to_go_episode)

        # for value function prediction
        assert len(reward_to_go) == len(obs_x)

        assert len(episode_sum_rewards) == len(obs_y)

        # for modelling
        assert len(obs_x) == len(act_x)

        # for reconstruction
        assert len(obs_x) == len(obs_y)
        profiler.end('episodes')
        profiler.start('convert')

        obs_x = np.array(obs_x)
        obs_y = np.array(obs_y)
        obs = np.array(obs)
        act_x = np.array(act_x)
        reward_to_go = np.array(reward_to_go)
        done_y = np.array(done_y)
        rew_y = np.array(rew_y)
        episode_sum_rewards = np.array(episode_sum_rewards)
        profiler.end('convert')

        context = {'obs_x': obs_x, 'obs_y': obs_y, 'action_x': act_x,
                   'rew_y': rew_y, 'done_y': done_y,
                   'obs': obs,
                   'reward_to_go': reward_to_go,
                   'episode_sum_rewards': episode_sum_rewards}

        profiler.start('multistep')

        for steps in self.data_multistep:
            n_step_ctx = get_multi_step_rl_context(self.collector, n_steps_forward=steps,
                                                   return_intermediate=True)
            for key, val in n_step_ctx.items():
                new_key_name = f"multistep_{steps}_{key}"
                assert isinstance(val, np.ndarray), f"multi-step value must be np.array {val}"
                assert new_key_name not in context, f"Key {new_key_name} already" \
                                                    f" in context {steps} {context.keys()}"
                if 'act' in key and self.to_onehot:
                    val = one_hot_encode_vectorized(n=self.action_shape[0],
                                                    values=val)
                context[new_key_name] = val
        profiler.end('multistep')

        return context


@gin.configurable
class ExperienceReplayBuffer():
    """Collect data from RL contexts, and store it. Then, sample batches."""
    def __init__(self, config,
                 collectors):
        self.config = config
        self.collectors = collectors
        self.n_collectors = len(self.collectors)
        self.buffer_steps = self.config.get('buffer_steps', 1000000)
        self.shuffle_together = get_shuffle_together(self.config)
        self.minibatch_size = self.config.get('minibatch_size', 5000)

        self.next_episode_refs = []
        self.future_episode_size = self.config.get('future_episode_size', 10)
        self.min_collected_sample_ratio = self.config.get('min_collected_sample_ratio', 0.01)

        self.reset()

    def reset(self):
        self.buffer = {}
        self.buffer_n = {}
        self.buffer_write_idx = {}
        self.steps_sampled = 0
        self.steps_collected = 0
        self.collect_one_iteration(min_batches=1)
        return {'info': 'buffer cleaned'}

    def collected_sampled_ratio(self, eps=1e-3):
        if self.steps_sampled == 0:
            return 1.
        return 1. * self.steps_collected / self.steps_sampled

    def collect_one_iteration(self, min_batches=1):
        while len(self.next_episode_refs) < max(min_batches, self.future_episode_size):
            remote_id = np.random.choice(self.n_collectors)
            ref = self.collectors[remote_id].collect_get_context.remote()
            self.next_episode_refs.append(ref)
            # logging.warning(f"Scheduling episode on {remote_id}")

        self.next_episode_refs = self.collect_from(self.next_episode_refs,
                                                   min_batches=min_batches)


    def collect(self, min_batches=1, enable_wait=True):
        steps_old = self.steps_collected

        t1 = time()
        iters = 0
        stats = {}
        while (self.collected_sampled_ratio() <= self.min_collected_sample_ratio) or not enable_wait:
            self.collect_one_iteration(min_batches=min_batches)
            iters += 1
            if not enable_wait:
                break
            elif iters >= 2:
                sleep(0.1)
        t2 = time()

        steps_new = self.steps_collected

        stats.update({
            'steps_collected': self.steps_collected,
            'steps_sampled': self.steps_sampled,
            'steps_collected_laps': 1. * self.steps_collected / self.buffer_steps,
            'steps_sampled_laps': 1. * self.steps_sampled / self.buffer_steps,
            'collected_sampled_ratio': self.collected_sampled_ratio(),
            'collect_time_s': t2 - t1,
            'collect_iters': iters,
            'pending_refs': len(self.next_episode_refs),
            'steps_collected_now': steps_new - steps_old,
        })
        return stats

    def collect_from(self, futures, min_batches=1):
        """Collect from futures. Returns list of unprocessed elements."""
        if min_batches == 0: # don't wait for anything, but get all available items
            f_ready, f_remaining = ray_wait_all_non_blocking(futures)
        else:
            f_ready, f_remaining = ray.wait(futures, num_returns=min_batches)

        for f in f_ready:
            # logging.warning("Collecting episode")
            self.observe(ray.get(f))
        return f_remaining

    def collect_local(self, collector):
        data = collector.collect_get_context()
        self.observe(data)

    def check_keys(self):
        assert self.buffer, "Buffer is empty"
        left_keys = set(self.buffer.keys())
        for group in self.shuffle_together:
            left_keys.difference_update(group)

            group_lens = [self.buffer_n[key] for key in group]
            group_len = group_lens[0]
            assert [group_len == l for l in
                    group_lens], f"group lens must be the same {group} {group_lens}"
            assert group_len <= self.buffer_steps, f"Too many steps" \
                                                   f" {group} {group_len} {self.buffer_steps}"
        assert not left_keys, f"Some keys were not used: {left_keys} {self.shuffle_together}"

    def sample_batch(self, max_size=None):
        p = TimeProfiler(enable=False)
        p.start('sample_batch')
        if max_size is None:
            max_size = self.minibatch_size
        elif max_size == -1:  # the whole buffer
            max_size = self.buffer_steps
        self.check_keys()
        pre_context_return = {}
        for group in self.shuffle_together:
            p.start(f'{group}_sample_idx')
            group_len = self.buffer_n[group[0]]
            if group_len > max_size:
                idxes_return = np.random.choice(a=group_len,
                                                size=max_size, replace=False)
            else:
                idxes_return = slice(-1)
            p.end(f'{group}_sample_idx')

            p.start(f'{group}_sample')
            for key in group:
                pre_context_return[key] = self.buffer[key][idxes_return]
            p.end(f'{group}_sample')

        self.steps_sampled += len(pre_context_return[self.shuffle_together[0][0]])
        p.end('sample_batch')
        p.report()
        return pre_context_return

    def observe(self, pre_context):
        """Write data to buffer."""
        for key in pre_context.keys():

            assert isinstance(pre_context[key], np.ndarray), f"Inputs must be numpy " \
                                                             f"arrays {key}" \
                                                             f" {type(pre_context[key])}"
            if key not in self.buffer:
                self.buffer[key] = np.zeros((self.buffer_steps, *pre_context[key].shape[1:]),
                                            dtype=np.float32)
                self.buffer_write_idx[key] = 0
                self.buffer_n[key] = 0
            else:
                idx_start = self.buffer_write_idx[key]
                L = len(pre_context[key])
                idx_end = idx_start + L

                # buffer wrap
                if idx_end >= self.buffer_steps:
                    # buffer is fully taken
                    self.buffer_write_idx[key] += L
                    self.buffer_write_idx[key] %= self.buffer_steps

                    part_1_L = self.buffer_steps - idx_start
                    part_2_L = L - part_1_L

                    self.buffer[key][idx_start:self.buffer_steps] = pre_context[key][:part_1_L]
                    self.buffer[key][:part_2_L] = pre_context[key][part_1_L:]
                    self.buffer_n[key] = self.buffer_steps
                else:
                    self.buffer[key][idx_start:idx_end] = pre_context[key]
                    self.buffer_write_idx[key] += L
                    self.buffer_n[key] += L

                self.buffer_n[key] = min(self.buffer_steps, self.buffer_n[key])

        self.steps_collected += len(pre_context[self.shuffle_together[0][0]])

@gin.configurable
class ParallelContextCollector():
    def __init__(self, config):
        RLContextRemote = ray.remote(RLContext)
        ExperienceReplayBufferRemote = ray.remote(ExperienceReplayBuffer)

        self.config = config
        self.n_collectors = self.config.get('n_collectors', 3)

        if self.n_collectors == 0:
            self.rl_context = RLContext(config=self.config,
                                        gin_config_str=gin.config_str())
            self.replay_buffer = ExperienceReplayBuffer(config=self.config,
                                                        collectors=[])

        else:
            self.remote_rl_contexts = [RLContextRemote.remote(config=self.config,
                                                              gin_config_str=gin.config_str())
                                       for _ in range(self.n_collectors)]
            self.replay_buffer = ExperienceReplayBufferRemote.remote(
                config=self.config, collectors=self.remote_rl_contexts)
            self.next_batch_refs = set()
            self.stats_ref = None


        self.future_batch_size = self.config.get('future_batch_size', 10)
        self.collect_initial()

    def __del__(self):
        if self.n_collectors:
            self.next_batch_refs = set()
            ray.kill(self.replay_buffer)

            for c in self.remote_rl_contexts:
                ray.kill(c)

    def collect_initial(self, do_tqdm=True):

        do_ascii = tune.get_trial_name() is not None
        colour = 'green'

        if self.n_collectors == 0:
            # resetting the local buffer
            info = self.replay_buffer.reset()
            logging.info(f"Buffer reset response: {info}")

            for _ in tqdm(range(self.future_batch_size),
                          disable=not do_tqdm, desc="Initial buffer fill [local]",
                          ascii=do_ascii, colour=colour):
                self.replay_buffer.collect_local(self.rl_context)
        else:
            # resetting the remote buffer
            info = ray.get(self.replay_buffer.reset.remote())
            logging.info(f"Buffer reset response: {info}")

            target = self.config.get('collect_initial_steps', 1000)
            collected = 0

            with tqdm(initial=collected, total=target, disable=not do_tqdm, desc="Initial buffer fill", ascii=do_ascii, colour=colour) as pbar:
                while collected < target:
                    stats = ray.get(self.replay_buffer.collect.remote(
                        min_batches=0, enable_wait=False))
                    delta = stats['steps_collected_now']

                    if delta:
                        pbar.update(delta)
                        pbar.set_postfix(**stats)
                        collected += delta
                    sleep(0.1)

    def get_whole_buffer(self):
        if self.n_collectors == 0:
            return self.replay_buffer.sample_batch(-1)
        else:
            return ray.get(self.replay_buffer.sample_batch.remote(-1))

    def collect_get_context(self):
#         logging.warning(f"Collecting data collect_get_context()...")
        if self.n_collectors == 0:
            for _ in range(self.future_batch_size):
                self.replay_buffer.collect_local(self.rl_context)

            return self.replay_buffer.sample_batch()
        else:
            # storing episodes into memory of the buffer
            # will collect at least one batch of data
            if self.stats_ref is None:
                self.stats_ref = self.replay_buffer.collect.remote(
                    min_batches=self.config.get('wait_for_batches_collected', 0),
                    enable_wait=True
                )

            stats_ready, _ = ray.wait([self.stats_ref], timeout=0)
            stats = {}
            if stats_ready:
                stats = ray.get(self.stats_ref)
                self.stats_ref = None

            # requesting shuffled batches
            while len(self.next_batch_refs) < self.future_batch_size:
                self.next_batch_refs.add(self.replay_buffer.sample_batch.remote())

            ready_refs, non_ready_refs = ray.wait(list(self.next_batch_refs), num_returns=1)
            ready_ref = ready_refs[0]
            self.next_batch_refs.remove(ready_ref)
            pre_context = ray.get(ready_ref)
            pre_context.update({f"context_stats_{x}": y for x, y in stats.items()})
            
#             logging.warning(f"Collecting data FINISHED collect_get_context()...")
            
            return pre_context
