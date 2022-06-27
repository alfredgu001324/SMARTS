import sys

sys.path.insert(0, "./")
import gym

import examples.env.figure_eight_v0_env
import zoo.policies.keep_lane_agent

from smarts.core.utils.episodes import episode_range
from smarts.env.wrappers.episode_logger import EpisodeLogger
from smarts.env.wrappers.record_video import RecordVideo
from smarts.zoo import registry

if __name__ == "__main__":
    # FormatObs should already be applied
    env = gym.make("figure_eight-v0")
    env: gym.Env = RecordVideo(
        env, video_folder="videos", video_length=40, step_trigger=lambda s: s % 100 == 0
    )
    env: gym.Env = EpisodeLogger(env)

    agent = registry.make_agent("zoo.policies:keep-lane-agent-v0")
    for episode in episode_range(max_steps=450):
        observation = env.reset()
        reward, done, info = None, False, None
        while episode.continues(observation, reward, done, info):
            action = agent.act(observation)
            observation, reward, done, info = env.step(action)

    env.close()
