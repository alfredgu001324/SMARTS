import numpy as np
import tensorflow as tf


class Vehicle:
    def __init__(self, name, config):
        self.name = name
        self._config = config
        self._gamma = config["gamma"]
        self.reset()

    def reset(self):
        self._states = []
        self._actions = []
        self._values = []
        self._rewards = []
        self._dones = []
        self._probs = []
        self._discounted_rewards = None
        self._advantages = None
        self._returns = None
        self._last_value = None
        self._old_probs = None  # Selected action's probability
        self._action_inds = None

    @property
    def states(self):
        return self._states

    @property
    def rewards(self):
        return self._rewards

    @property
    def advantages(self):
        return self._advantages

    @property
    def discounted_rewards(self):
        return self._discounted_rewards

    @property
    def actions(self):
        return self._actions

    @property
    def probs(self):
        return self._probs

    @property
    def old_probs(self):
        return self._old_probs

    @old_probs.setter
    def old_probs(self, x):
        self._old_probs = x

    @property
    def action_inds(self) -> tf.TensorSpec(shape=(None, 2), dtype=tf.dtypes.int32):
        return self._action_inds

    @action_inds.setter
    def action_inds(self, x):
        self._action_inds = x

    def add_trajectory(self, action, value, state, done, prob, reward):
        self._states.append(state)
        self._actions.append(action)
        self._values.append(value)
        self._probs.append(prob)
        self._dones.append(done)
        self._rewards.append(reward)

    def add_last_transition(self, value):
        self._last_value = value

    def compute_advantages(self):
        discounted_rewards = np.array(self._rewards + [self._last_value])

        for t in reversed(range(len(self._rewards))):
            discounted_rewards[t] = self._rewards[t] + self._gamma * discounted_rewards[
                t + 1
            ] * (1 - self._dones[t])

        discounted_rewards = discounted_rewards[:-1]

        # advantages are bootstrapped discounted rewards - values, using Bellman's equation
        advantages = discounted_rewards - self._values
        # standardise advantages
        advantages -= np.mean(advantages)
        advantages /= np.std(advantages) + 1e-10
        # standardise rewards too
        # discounted_rewards -= np.mean(discounted_rewards)
        # discounted_rewards /= np.std(discounted_rewards) + 1e-8

        self._discounted_rewards = discounted_rewards
        self._advantages = advantages