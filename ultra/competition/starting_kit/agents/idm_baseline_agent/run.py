# MIT License
#
# Copyright (C) 2021. Huawei Technologies Co., Ltd. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import argparse
from typing import List

import gym

import agent as baseline_agent


def run(scenarios: List[str], episodes: int, headless: bool, seed: int = 1):
    AGENT_ID = "AGENT-007"

    agent_spec = baseline_agent.agent_spec

    env = gym.make(
        "ultra.env:ultra-v0",
        agent_specs={AGENT_ID: agent_spec},
        scenario_info=scenarios,
        headless=headless,
        seed=seed,
        timestep_sec=0.1,
        ordered_scenarios=True,
    )
    agent = agent_spec.build_agent()

    for episode_index in range(episodes):
        print(f"Starting episode #{episode_index + 1}")
        total_reward = 0.0
        dones = {"__all__": False}
        observations = env.reset()
        print(f"Scenario: {env.scenario_log['scenario_map']}")
        while not dones["__all__"]:
            action = agent.act(observations[AGENT_ID])
            observations, rewards, dones, infos = env.step({AGENT_ID: action})
            total_reward += rewards[AGENT_ID]
        print(f"Return (sum of rewards): {total_reward}")
        print(f"Reached goal? {infos[AGENT_ID]['logs']['events'].reached_goal}")

    env.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser("random-agent-example")
    parser.add_argument(
        "scenarios",
        help=(
            "A list of scenarios. Each element can be either the scenario to run, or a "
            "directory of scenarios to sample from."
        ),
        type=str,
        nargs="+",
    )
    parser.add_argument(
        "--episodes",
        help="The number of episodes to run the experiment.",
        type=int,
        default=1,
    )
    parser.add_argument(
        "--headless",
        help="Run the experiment without Envision.",
        action="store_true",
    )
    args = parser.parse_args()

    run(args.scenarios, args.episodes, args.headless)