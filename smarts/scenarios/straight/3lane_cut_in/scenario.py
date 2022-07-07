# Copyright (C) 2022. Huawei Technologies Co., Ltd. All rights reserved.
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
# FITNESS FOR A PARTICULAR PURPOSE AND NON-INFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.

import random
from itertools import combinations
from pathlib import Path

from smarts.sstudio import gen_scenario
from smarts.sstudio.types import (
    Flow,
    LaneChangingModel,
    Mission,
    Route,
    Scenario,
    Traffic,
    TrafficActor,
)

normal = TrafficActor(
    name="car",
    lane_changing_model=LaneChangingModel(
        pushy=1,
        impatience=1,
        cooperative=0.1,
        speed_Gain=1,
    ),
)

# See SUMO doc
# Lane changing model
# https://sumo.dlr.de/docs/Definition_of_Vehicles%2C_Vehicle_Types%2C_and_Routes.html#lane-changing_models
# Junction model
# https://sumo.dlr.de/docs/Definition_of_Vehicles%2C_Vehicle_Types%2C_and_Routes.html#junction_model_parameters

# cooperative = TrafficActor(
#     name="cooperative",
#     speed=Distribution(sigma=0.3, mean=1.0),
#     lane_changing_model=LaneChangingModel(
#         pushy=0.1,
#         impatience=0.1,
#         cooperative=0.9,
#         speed_Gain=0.8,
#     ),
#     junction_model=JunctionModel(
#         impatience=0.1,
#     ),
# )
# aggressive = TrafficActor(
#     name="aggressive",
#     speed=Distribution(sigma=0.3, mean=1.0),
#     lane_changing_model=LaneChangingModel(
#         pushy=0.8,
#         impatience=1,
#         cooperative=0.1,
#         speed_Gain=2.0,
#     ),
#     junction_model=JunctionModel(
#         impatience=0.6,
#     ),
# )

# flow_name = (start_lane, end_lane,)
route_opt = [
    (0, 0),
    (0, 1),
    (0, 2),
    (1, 0),
    (1, 1),
    (1, 2),
    (2, 0),
    (2, 1),
    (2, 2),
]

min_flows = 3
max_flows = 7
route_comb = [
    com
    for elems in range(min_flows, max_flows + 1)
    for com in combinations(route_opt, elems)
]

traffic = {}
for name, routes in enumerate(route_comb):
    traffic[str(name)] = Traffic(
        flows=[
            Flow(
                route=Route(
                    begin=("E1", r[0], 0),
                    # via=("E0"),
                    end=("E2", r[1], "max"),
                ),
                # Random flow rate, between x and y vehicles per minute.
                rate=60 * random.uniform(10, 20),
                # Random flow start time, between x and y seconds.
                begin=random.uniform(0, 5),
                # For an episode with maximum_episode_steps=3000 and step
                # time=0.1s, maximum episode time=300s. Hence, traffic set to
                # end at 900s, which is greater than maximum episode time of
                # 300s.
                end=60 * 15,
                actors={normal: 1},
                randomly_spaced=True,
            )
            for r in routes
        ]
    )

ego_missions = [
    Mission(
        Route(begin=("E1", 1, 10), end=("E2", 1, "max")),
        start_time=14,
    )
]

gen_scenario(
    scenario=Scenario(
        traffic=traffic,
        ego_missions=ego_missions,
    ),
    output_dir=Path(__file__).parent,
)
