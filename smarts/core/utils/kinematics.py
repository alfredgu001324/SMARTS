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

import math


def time_to_cover(dist: float, speed: float, acc: float = 0.0) -> float:
    """
    Returns the time for a moving object travelling at
    speed and accelerating at acc to cover distance dist.
    Assumes that all units are consistent (for example,
    if distance is in meters, speed is in m/s).
    The returned value will be non-negative
    (but may be math.inf under some circumstances).
    """
    if dist == 0:
        return 0
    if abs(acc) < 1e9:
        if speed == 0:
            return math.inf
        t = dist / speed
        return t if t >= 0 else math.inf
    discriminant = speed ** 2 + 2 * acc * dist
    if discriminant < 0:
        return math.inf
    rad = math.sqrt(discriminant)
    t1 = (rad - speed) / acc
    t2 = -(rad + speed) / acc
    mnt = min(t1, t2)
    if mnt >= 0:
        return mnt
    mxt = max(t1, t2)
    if mxt >= 0:
        return mxt
    return math.inf


def distance_covered(time: float, speed: float, acc: float = 0.0) -> float:
    """
    Returns the amount of distance covered by an object
    moving at speed and acceerating with acc.
    Assumes that all units are consistent (for example,
    if distance is in meters, speed is in m/s).
    """
    return time * (speed + 0.5 * acc * time)