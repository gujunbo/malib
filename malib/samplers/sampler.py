import numpy as np

from malib.logger import logger, tabular
from PIL import Image
import os
import subprocess


def render(env, filepath, episode_step, stitch=False):
    frame = env.render(mode="rgb_array")
    # Image.fromarray(frame).save(filepath + "." + ("%02d" % episode_step) + ".bmp")
    # if stitch:
    #     subprocess.run(["ffmpeg", "-v", "warning", "-r", "10", "-i", filepath + ".%02d.bmp", "-vcodec", "mpeg4", "-y", filepath + ".mp4"], shell=False, check=True)
    #     subprocess.run(["rm -f " + filepath + ".*.bmp"], shell=True, check=True)


class Sampler(object):
    def __init__(self, max_path_length, min_pool_size, batch_size):
        self._max_path_length = max_path_length
        self._min_pool_size = min_pool_size
        self._batch_size = batch_size

        self.env = None
        self.policy = None
        self.pool = None

    def initialize(self, env, policy, pool):
        self.env = env
        self.policy = policy
        self.pool = pool

    def set_policy(self, policy):
        self.policy = policy

    def sample(self):
        raise NotImplementedError

    def batch_ready(self):
        enough_samples = self.pool.size >= self._min_pool_size
        return enough_samples

    def random_batch(self):
        return self.pool.random_batch(self._batch_size)

    def terminate(self):
        self.env.terminate()

    def log_diagnostics(self):
        logger.record_tabular("pool-size", self.pool.size)


class MASampler(Sampler):
    def __init__(
        self,
        agent_num,
        max_path_length=20,
        min_pool_size=10e4,
        batch_size=64,
        global_reward=False,
        **kwargs
    ):
        # super(MASampler, self).__init__(**kwargs)
        self.agent_num = agent_num
        self._max_path_length = max_path_length
        self._min_pool_size = min_pool_size
        self._batch_size = batch_size
        self._global_reward = global_reward
        self._path_length = 0
        self._path_return = np.zeros(self.agent_num)
        self._last_path_return = np.zeros(self.agent_num)
        self._max_path_return = np.array([-np.inf] * self.agent_num, dtype=np.float32)
        self._n_episodes = 0
        self._total_samples = 0
        # self.episode_rewards = [0]  # sum of rewards for all agents
        # self.agent_rewards = [[0] for _ in range(self.agent_num)] # individual agent reward
        self.step = 0
        self._current_observation_n = None
        self.env = None
        self.agents = None

    def set_policy(self, policies):
        for agent, policy in zip(self.agents, policies):
            agent.policy = policy

    def batch_ready(self):
        enough_samples = (
            max(agent.replay_buffer.size for agent in self.agents)
            >= self._min_pool_size
        )
        return enough_samples

    def random_batch(self, i):
        return self.agents[i].pool.random_batch(self._batch_size)

    def initialize(self, env, agents):
        self._current_observation_n = None
        self.env = env
        self.agents = agents

    def sample(self, explore=False):
        self.step += 1
        if self._current_observation_n is None:
            self._current_observation_n = self.env.reset()
        action_n = []
        if explore:
            action_n = self.env.action_spaces.sample()
        else:
            for agent, current_observation in zip(
                self.agents, self._current_observation_n
            ):
                action = agent.act(current_observation.astype(np.float32))
                action_n.append(np.array(action))

        action_n = np.asarray(action_n)

        next_observation_n, reward_n, done_n, info = self.env.step(action_n)
        if self._global_reward:
            reward_n = np.array([np.sum(reward_n)] * self.agent_num)

        self._path_length += 1
        self._path_return += np.array(reward_n, dtype=np.float32)
        self._total_samples += 1
        for i, agent in enumerate(self.agents):
            opponent_action = action_n[
                [j for j in range(len(action_n)) if j != i]
            ].flatten()
            agent.replay_buffer.add_sample(
                observation=self._current_observation_n[i].astype(np.float32),
                action=action_n[i].astype(np.float32),
                reward=reward_n[i].astype(np.float32),
                terminal=done_n[i],
                next_observation=next_observation_n[i].astype(np.float32),
                opponent_action=opponent_action.astype(np.float32),
            )

        self._current_observation_n = next_observation_n

        if np.all(done_n) or self._path_length >= self._max_path_length:
            self._current_observation_n = self.env.reset()
            self._max_path_return = np.maximum(self._max_path_return, self._path_return)
            self._mean_path_return = self._path_return / self._path_length
            self._last_path_return = self._path_return
            self._path_length = 0
            self._path_return = np.zeros(self.agent_num)
            self._n_episodes += 1
            self.log_diagnostics()
            logger.log(tabular)
            logger.dump_all()
        else:
            self._current_observation_n = next_observation_n

    def log_diagnostics(self):
        for i in range(self.agent_num):
            tabular.record(
                "max-path-return_agent_{}".format(i), self._max_path_return[i]
            )
            tabular.record(
                "mean-path-return_agent_{}".format(i), self._mean_path_return[i]
            )
            tabular.record(
                "last-path-return_agent_{}".format(i), self._last_path_return[i]
            )
        tabular.record("episodes", self._n_episodes)
        tabular.record("episode_reward", self._n_episodes)
        tabular.record("total-samples", self._total_samples)


class SingleSampler(Sampler):
    def __init__(self, max_path_length, min_pool_size=10e4, batch_size=64, **kwargs):
        self._max_path_length = max_path_length
        self._min_pool_size = min_pool_size
        self._batch_size = batch_size
        self._path_length = 0
        self._path_return = np.zeros(1)
        self._last_path_return = np.zeros(1)
        self._max_path_return = np.array([-np.inf], dtype=np.float32)
        self._n_episodes = 0
        self._total_samples = 0
        self.step = 0
        self._current_observation = None
        self.env = None
        self.agent = None

        # FIXME : temporary recorder for boat game, delete it afterwards.
        self.episode_rewards = []
        self.episode_positions = []

    def set_policy(self, policy):
        self.agent.policy = policy

    def batch_ready(self):
        enough_samples = max(self.agent.replay_buffer.size) >= self._min_pool_size
        return enough_samples

    def random_batch(self):
        return self.agent.pool.random_batch(self._batch_size)

    def initialize(self, env, agent):
        self._current_observation = None
        self.env = env
        self.agent = agent

    def sample(self, explore=False):
        self.step += 1
        if self._current_observation is None:
            self._current_observation = self.env.reset()
        self._current_observation = np.squeeze(self._current_observation).flatten()

        if explore:
            action = self.env.action_space.sample()
        else:
            action = self.agent.act(np.squeeze(self._current_observation).flatten())

        action = np.asarray(action)
        next_observation, reward, done, info = self.env.step(action)
        next_observation = np.squeeze(next_observation).flatten()
        reward = np.squeeze(reward).flatten()
        action = np.squeeze(action).flatten()
        done = np.squeeze(done)
        done = done.astype(np.int8)

        self._path_length += 1
        self._path_return += np.mean(reward)
        self._total_samples += 1
        self.agent.replay_buffer.add_sample(
            observation=self._current_observation,
            action=action,
            reward=reward,
            terminal=done,
            next_observation=next_observation,
        )

        self._current_observation = next_observation

        if np.all(done) or self._path_length >= self._max_path_length:
            self._max_path_return = np.maximum(self._max_path_return, self._path_return)
            self._mean_path_return = self._path_return / self._path_length
            self._last_path_return = self._path_return
            self._terminal_position = self._current_observation

            self._current_observation = self.env.reset()
            self._path_length = 0
            self._path_return = np.zeros(1)
            self._n_episodes += 1

            # FIXME : delete it afterwards.
            if explore is False:
                self.episode_rewards.append(self._last_path_return.item())
                self.episode_positions.append(
                    [
                        self._terminal_position[0].item(),
                        self._terminal_position[1].item(),
                    ]
                )

            self.log_diagnostics()
            logger.log(tabular)
            logger.dump_all()

        else:
            self._current_observation = next_observation

    def log_diagnostics(self):
        tabular.record("max-path-return_agent", self._max_path_return[0])
        tabular.record("mean-path-return_agent", self._mean_path_return[0])
        tabular.record("last-path-return_agent", self._last_path_return[0])
        tabular.record("episodes", self._n_episodes)
        tabular.record("episode_reward", self._n_episodes)
        tabular.record("terminal_position_x", self._terminal_position[0])
        tabular.record("terminal_position_y", self._terminal_position[1])
        tabular.record("total-samples", self._total_samples)
