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

import logging
import math
import random
import xml.etree.ElementTree as XET
from collections import deque
from functools import lru_cache
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import numpy as np
from cached_property import cached_property
from shapely.affinity import rotate as shapely_rotate
from shapely.geometry import Polygon, box as shapely_box

from .controllers import ActionSpaceType
from .coordinates import Dimensions, Heading, Point, Pose, RefLinePoint
from .provider import ProviderRecoveryFlags, ProviderState
from .road_map import RoadMap, Waypoint
from .scenario import Scenario
from .traffic_provider import TrafficProvider
from .utils.kinematics import time_to_cover
from .utils.math import (
    min_angles_difference_signed,
    radians_to_vec,
    vec_to_radians,
)
from .vehicle import ActorRole, VEHICLE_CONFIGS, VehicleState


# TODO:  debug traffic jams
# TODO:  add tests:
# TODO:     - test_traffic_simulation.py
# TODO:     - bubble hijacking
# TODO:     - reserved area tests
# TODO:  left turns across traffic and other basic uncontrolled intersection stuff
# TODO:  dynamic routing
# TODO:  test mixed:  Smarts+Sumo
# TODO:      - if using pre-generated rou files that both Sumo and Smarts can support, then make traffic sim contructors take this path (and remove "engine")
# TODO:  profile
# TODO:  failing pytests (determinism?)
# TODO:  refactor MPP and TIP into Controllers
# TODO:  reconsider vehicle dims stuff from proposal
# TODO:  consider lane markings
# TODO:  consider traffic lights and intersection right-of-way


class LocalTrafficProvider(TrafficProvider):
    """
    A LocalTrafficProvider simulates multiple traffic actors on a generic RoadMap.
    Args:
        endless_traffic:
            Reintroduce vehicles that exit the simulation.
    """

    def __init__(self, endless_traffic: bool = True):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._endless_traffic: bool = endless_traffic
        self._scenario = None
        self.road_map: RoadMap = None
        self._flows: Dict[str, Dict[str, Any]] = dict()
        self._my_actors: Dict[str, _TrafficActor] = dict()
        self._other_vehicles: Dict[
            str, Tuple[VehicleState, Optional[Sequence[str]]]
        ] = dict()
        self._reserved_areas: Dict[str, Polygon] = dict()
        self._route_lane_lengths: Dict[int, Dict[Tuple[str, int], float]] = dict()
        self._actors_created: int = 0
        self._nearest_lanes_cache: Dict[str, RoadMap.Lane] = dict()
        self._offsets_cache: Dict[str, Dict[str, float]] = dict()

    @property
    def action_spaces(self) -> Set[ActionSpaceType]:
        return set()

    def manages_vehicle(self, vehicle_id: str) -> bool:
        return vehicle_id in self._my_actors

    def _cache_route_lengths(self, route: Sequence[str]) -> int:
        route_id = hash(tuple(route))
        if route_id in self._route_lane_lengths:
            return route_id
        # TAI: could pre-cache curvatures here too (like waypoints) ?
        self._route_lane_lengths[route_id] = dict()

        def _backprop_length(bplane: RoadMap.Lane, length: float, rind: int):
            assert rind >= 0
            rind -= 1
            for il in bplane.incoming_lanes:
                il = il.composite_lane
                ill = self._route_lane_lengths[route_id].get((il.lane_id, rind))
                if ill is not None:
                    self._route_lane_lengths[route_id][(il.lane_id, rind)] = (
                        ill + length
                    )
                    _backprop_length(il, length, rind)

        for r_ind, road_id in enumerate(route):
            road = self.road_map.road_by_id(road_id)
            assert road, f"route road '{road_id}' not found in road map"
            for lane in road.lanes:
                lane = lane.composite_lane
                assert (lane.lane_id, r_ind) not in self._route_lane_lengths[route_id]
                _backprop_length(lane, lane.length, r_ind)
                self._route_lane_lengths[route_id][(lane.lane_id, r_ind)] = lane.length
            else:
                continue
            break
        if not road:
            return route_id
        # give lanes that would form a loop an advantage...
        for lane in road.lanes:
            lane = lane.composite_lane
            for og in lane.outgoing_lanes:
                if (
                    og.road.road_id == route[0]
                    or og.road.composite_road.road_id == route[0]
                ):
                    self._route_lane_lengths[route_id][(lane.lane_id, r_ind)] += 1
        return route_id

    def _load_traffic_flows(self, traffic_spec: str):
        vtypes = {}
        routes = {}
        root = XET.parse(traffic_spec).getroot()
        assert root.tag == "routes"
        for child in root:
            if child.tag == "vType":
                vid = child.attrib["id"]
                vtypes[vid] = child.attrib
            elif child.tag == "route":
                rid = child.attrib["id"]
                routes[rid] = child.attrib["edges"]
            elif child.tag == "flow":
                flow = child.attrib
                vtype = vtypes.get(flow["type"])
                assert vtype, f"undefined vehicle type {flow['type']} used in flow"
                flow["vtype"] = vtype
                route = routes.get(flow["route"])
                assert route, f"undefined route {flow['route']} used in flow"
                route = route.split()
                flow["route"] = route
                flow["begin"] = float(flow["begin"])
                flow["end"] = float(flow["end"])
                flow["emit_period"] = (60.0 * 60.0) / float(flow["vehsPerHour"])
                self._flows[str(flow["id"])] = flow
                flow["route_id"] = self._cache_route_lengths(route)

    def _add_actor_in_flow(self, flow: Dict[str, Any]) -> bool:
        new_actor = _TrafficActor.from_flow(flow, self)
        new_actor_bbox = new_actor.bbox(True)
        for reserved_area in self._reserved_areas.values():
            if reserved_area.intersects(new_actor_bbox):
                return False
        # TODO:  also don't appear on any other vehicles either!  (TAI: but for other_vehicles, we can't cache their bbox polygons)
        for actor in self._my_actors.values():
            if actor.bbox().intersects(new_actor_bbox):
                return False
        self._my_actors[new_actor.actor_id] = new_actor
        self._logger.info(f"traffic actor {new_actor.actor_id} entered simulation")
        return True

    def _add_actors_for_time(self, sim_time: float):
        for flow in self._flows.values():
            if not flow["begin"] <= sim_time < flow["end"]:
                continue
            last_added = flow.get("last_added")
            if last_added is None or sim_time - last_added >= flow["emit_period"]:
                if self._add_actor_in_flow(flow):
                    flow["last_added"] = sim_time

    @property
    def _my_actor_states(self) -> List[VehicleState]:
        return [actor.state for actor in self._my_actors.values()]

    @property
    def _other_vehicle_states(self) -> List[VehicleState]:
        return [other for other, _ in self._other_vehicles.values()]

    @property
    def _all_states(self) -> List[VehicleState]:
        return self._my_actor_states + self._other_vehicle_states

    @property
    def _provider_state(self) -> ProviderState:
        return ProviderState(vehicles=self._my_actor_states)

    def setup(self, scenario: Scenario) -> ProviderState:
        self._scenario = scenario
        self.road_map = scenario.road_map
        traffic_specs = [
            ts for ts in self._scenario.traffic_specs if ts.endswith(".smarts.xml")
        ]
        assert len(traffic_specs) <= 1
        if traffic_specs:
            self._load_traffic_flows(traffic_specs[0])
        # TAI: is there any point if not?
        self._add_actors_for_time(0.0)
        return self._provider_state

    def step(self, actions, dt: float, elapsed_sim_time: float) -> ProviderState:
        self._add_actors_for_time(elapsed_sim_time)
        for other, _ in self._other_vehicles.values():
            if other.vehicle_id in self._reserved_areas:
                del self._reserved_areas[other.vehicle_id]

        # precompute nearest lanes for all vehicles and cache
        # (this prevents having to do it O(ovs^2) times)
        self._nearest_lanes_cache = dict()
        for ovs in self._all_states:
            self._nearest_lanes_cache[ovs.vehicle_id] = self.road_map.nearest_lane(
                ovs.pose.point, radius=ovs.dimensions.length
            )
        self._offsets_cache = dict()

        # Do state update in two passes so that we don't use next states in the
        # computations for actors encountered later in the iterator.
        for actor in self._my_actors.values():
            actor.compute_next_state(dt, self._all_states)

        dones = []
        for actor in self._my_actors.values():
            actor.step(dt)
            # TAI: consider removing vehicles that are off route too
            if actor.finished_route:
                dones.append(actor.actor_id)
        for actor_id in dones:
            del self._my_actors[actor_id]

        return self._provider_state

    def sync(self, provider_state: ProviderState):
        missing = self._my_actors.keys() - {
            psv.vehicle_id for psv in provider_state.vehicles
        }
        for left in missing:
            self._logger.warning(
                f"locally provided actor '{left}' disappeared from simulation"
            )
            del self._my_actors[left]
        hijacked = self._my_actors.keys() & {
            psv.vehicle_id
            for psv in provider_state.vehicles
            if psv.source != self.source_str
        }
        for jack in hijacked:
            self.stop_managing(jack)
        self._other_vehicles = dict()
        for vs in provider_state.vehicles:
            my_actor = self._my_actors.get(vs.vehicle_id)
            if my_actor:
                assert vs.source == self.source_str
                my_actor.state = vs
            else:
                assert vs.source != self.source_str
                self._other_vehicles[vs.vehicle_id] = (vs, None)

    def reset(self):
        # Unify interfaces with other providers
        pass

    def teardown(self):
        self._my_actors = dict()
        self._other_vehicles = dict()
        self._reserved_areas = dict()

    def destroy(self):
        pass

    def stop_managing(self, vehicle_id: str):
        # called when agent hijacks this vehicle
        assert (
            vehicle_id in self._my_actors
        ), f"stop_managing() called for non-tracked vehicle id '{vehicle_id}'"
        del self._my_actors[vehicle_id]

    def reserve_traffic_location_for_vehicle(
        self,
        vehicle_id: str,
        reserved_location: Polygon,
    ):
        self._reserved_areas[vehicle_id] = reserved_location

    def update_route_for_vehicle(self, vehicle_id: str, new_route_roads: Sequence[str]):
        traffic_actor = self._my_actors.get(vehicle_id)
        if traffic_actor:
            route_id = self._cache_route_lengths(new_route_roads)
            traffic_actor.update_route(route_id, new_route_roads)
            return
        other = self._other_vehicles.get(vehicle_id)
        if other:
            self._other_vehicles[vehicle_id] = (other[0], new_route_roads)
            return
        assert False, f"unknown vehicle_id: {vehicle_id}"

    def vehicle_dest_road(self, vehicle_id: str) -> Optional[str]:
        traffic_actor = self._my_actors.get(vehicle_id)
        if traffic_actor:
            return traffic_actor.route[-1]
        other = self._other_vehicles.get(vehicle_id)
        if other:
            return other[1][-1] if other[1] else None
        assert False, f"unknown vehicle_id: {vehicle_id}"
        return None

    def _cached_lane_offset(self, vs: VehicleState, lane: RoadMap.Lane):
        lane_offsets = self._offsets_cache.setdefault(vs.vehicle_id, dict())
        return lane_offsets.setdefault(
            lane.lane_id, lane.offset_along_lane(vs.pose.point)
        )

    def can_accept_vehicle(self, state: VehicleState) -> bool:
        return state.role == ActorRole.Social or state.role == ActorRole.Unknown

    def add_vehicle(
        self,
        provider_vehicle: VehicleState,
        route: Optional[Sequence[RoadMap.Route]] = None,
    ):
        provider_vehicle.source = self.source_str
        provider_vehicle.role = ActorRole.Social
        xfrd_actor = _TrafficActor.from_state(provider_vehicle, self, route)
        self._my_actors[xfrd_actor.actor_id] = xfrd_actor
        self._logger.info(
            f"traffic actor {xfrd_actor.actor_id} transferred to {self.source_str}."
        )


# TAI:  inner class?
class _TrafficActor:
    """Simulates a vehicle managed by the LocalTrafficProvider."""

    def __init__(self, flow: Dict[str, Any], owner: LocalTrafficProvider):
        self._logger = logging.getLogger(self.__class__.__name__)
        self._logger.setLevel(logging.INFO)

        self._owner = owner
        self._all_vehicle_states: Sequence[VehicleState] = []
        self._flow: Dict[str, Any] = flow
        self._vtype: Dict[str, Any] = flow["vtype"]
        self._route_ind: int = 0
        self._done_with_route: bool = False
        self._off_route: bool = False
        self._route: Sequence[str] = flow["route"]
        self._route_id: int = flow["route_id"]

        self._lane_speed: Dict[int, Tuple[float, float]] = dict()
        self._lane_windows: Dict[int, _TrafficActor._LaneWindow] = dict()
        self._lane_win: _TrafficActor._LaneWindow = None
        self._target_lane_win: _TrafficActor._LaneWindow = None

        self._min_space_cush = float(self._vtype.get("minGap", 2.5))
        speed_factor = float(self._vtype.get("speedFactor", 1.0))
        speed_dev = float(self._vtype.get("speedDev", 0.1))
        self._speed_factor = random.gauss(speed_factor, speed_dev)

        self._cutting_into = None
        self._in_front_after_cutin_secs = 0
        self._cutin_hold_secs = 3
        self._target_cutin_gap = 2.5 * self._min_space_cush
        self._aggressiveness = float(self._vtype.get("lcAssertive", 1.0))
        if self._aggressiveness <= 0:
            self._log.warning(
                "non-positive value {self._aggressiveness} for 'assertive' lane-changing parameter will be ignored"
            )
            self._aggressiveness = 1.0
        self._cutin_prob = float(self._vtype.get("lcCutinProb", 0.0))
        if not 0.0 <= self._cutin_prob <= 1.0:
            self._log.warning(
                "illegal probability {self._cutin_prob} for 'cutin_prob' lane-changing parameter will be ignored"
            )
            self._cutin_prob = 0.0

        self._owner._actors_created += 1

    @classmethod
    def from_flow(cls, flow: Dict[str, Any], owner: LocalTrafficProvider):
        """Factory to construct a _TrafficActor object from a flow dictionary."""
        vclass = flow["vtype"]["vClass"]
        dimensions = VEHICLE_CONFIGS[vclass].dimensions
        vehicle_type = vclass if vclass != "passenger" else "car"

        new_actor = cls(flow, owner)
        new_actor._lane, new_actor._offset = new_actor._resolve_flow_pos(
            flow, "depart", dimensions
        )
        position = new_actor._lane.from_lane_coord(RefLinePoint(s=new_actor._offset))
        heading = vec_to_radians(
            new_actor._lane.vector_at_offset(new_actor._offset)[:2]
        )
        init_speed = new_actor._resolve_flow_speed(flow)
        new_actor._state = VehicleState(
            vehicle_id=f"{new_actor._vtype['id']}-{new_actor._owner._actors_created}",
            pose=Pose.from_center(position, Heading(heading)),
            dimensions=dimensions,
            vehicle_type=vehicle_type,
            vehicle_config_type=vclass,
            speed=init_speed,
            linear_acceleration=np.array((0.0, 0.0, 0.0)),
            source=new_actor._owner.source_str,
            role=ActorRole.Social,
        )
        new_actor._dest_lane, new_actor._dest_offset = new_actor._resolve_flow_pos(
            flow, "arrival", dimensions
        )
        return new_actor

    @classmethod
    def from_state(
        cls,
        state: VehicleState,
        owner: LocalTrafficProvider,
        route: Optional[RoadMap.Route],
    ):
        """Factory to construct a _TrafficActor object from an existing VehiclState object."""
        if not route:
            route = owner.road_map.random_route()
        route_roads = [road.road_id for road in route.roads]
        route_id = owner._cache_route_lengths(route_roads)
        flow = dict()
        flow["vtype"] = dict()
        flow["route"] = route_roads
        flow["route_id"] = route_id
        flow["arrivalLane"] = "0"
        flow["arrivalPos"] = "random"
        # use default values for everything else in flow dict(s)...
        new_actor = _TrafficActor(flow, owner)
        new_actor.state = state
        new_actor._lane = owner.road_map.nearest_lane(state.pose.point)
        new_actor._offset = new_actor._lane.offset_along_lane(state.pose.point)
        new_actor._dest_lane, new_actor._dest_offset = new_actor._resolve_flow_pos(
            flow, "arrival", state.dimensions.length
        )
        return new_actor

    def _resolve_flow_pos(
        self, flow: Dict[str, Any], depart_arrival: str, dimensions: Dimensions
    ) -> Tuple[RoadMap.Lane, float]:
        base_err = (
            f"scenario traffic specifies flow with invalid route {depart_arrival} point"
        )
        road_id = self._route[0] if depart_arrival == "depart" else self._route[-1]
        road = self._owner.road_map.road_by_id(road_id)
        if not road:
            raise Exception(f"{base_err}:  road_id '{road_id}' not in map.")
        lane_ind = int(flow[f"{depart_arrival}Lane"])
        if not 0 <= lane_ind < len(road.lanes):
            raise Exception(
                f"{base_err}:  lane index {lane_ind} invalid for road_id '{road_id}'."
            )
        lane = road.lanes[lane_ind]
        offset = flow[f"{depart_arrival}Pos"]
        if offset == "max":
            offset = lane.length
            if depart_arrival == "depart":
                offset -= 0.5 * dimensions
        elif offset == "random":
            offset = random.random() * lane.length
        elif 0 <= float(offset) <= lane.length:
            offset = float(offset)
        else:
            raise Exception(
                f"{base_err}:  starting offset {offset} invalid for road_id '{road_id}'."
            )
        # convert to composite system...
        target_pt = lane.from_lane_coord(RefLinePoint(s=offset))
        lane = lane.composite_lane
        offset = lane.offset_along_lane(target_pt)
        return (lane, offset)

    def _resolve_flow_speed(self, flow: Dict[str, Any]) -> float:
        depart_speed = flow.get("departSpeed", 0.0)
        max_speed = float(self._vtype.get("maxSpeed", 55.5))
        if depart_speed == "random":
            return random.random() * max_speed
        elif depart_speed == "max":
            return min(max_speed, self._lane.speed_limit)
        elif depart_speed == "speedLimit":
            if self._lane.speed_limit is not None:
                return self._lane.speed_limit
            else:
                raise Exception(
                    f"scenario specifies departSpeed='speed_limit' but no speed limit defined for lane '{self._lane.lane_id}'."
                )
        departSpeed = float(depart_speed)
        assert departSpeed >= 0
        return departSpeed

    @property
    def state(self) -> VehicleState:
        """Returns the current VehicleState for this actor."""
        return self._state

    @state.setter
    def state(self, state: VehicleState):
        """Sets the current VehicleState for this actor."""
        self._state = state
        self.bbox.cache_clear()

    @property
    def actor_id(self) -> str:
        """A unique id identifying this actor."""
        return self._state.vehicle_id

    @property
    def route(self) -> Sequence[str]:
        """The route (sequence of road_ids) this actor will attempt to take."""
        return self._route

    def update_route(self, route_id: int, route: Sequence[str]):
        """Update the route (sequence of road_ids) this actor will attempt to take.
        A unique route_id is provided for referencing the route cache in he owner provider."""
        self._route = route
        self._route_id = route_id
        self._dest_lane, self._dest_offset = self._resolve_flow_pos(
            self._flow, "arrival", self._state.dimensions.length
        )
        self._route_ind = 0

    @property
    def finished_route(self) -> bool:
        """Returns True iff this vehicle has reached the end of its route."""
        return self._done_with_route

    @property
    def off_route(self) -> bool:
        """Returns True iff this vehicle has left its route before it got to the end."""
        return self._off_route

    @property
    def lane(self) -> RoadMap.Lane:
        """Returns the current Lane object."""
        return self._lane

    @property
    def road(self) -> RoadMap.Road:
        """Returns the current Road object."""
        return self._lane.road

    @property
    def offset_along_lane(self) -> float:
        """Returns the current offset along the current Lane object."""
        return self._offset

    @property
    def speed(self) -> float:
        """Returns the current speed."""
        return self._state.speed

    @property
    def acceleration(self) -> float:
        """Returns the current (linear) acceleration."""
        if self._state.linear_acceleration is None:
            return 0.0
        return np.linalg.norm(self._state.linear_acceleration)

    @lru_cache(maxsize=2)
    def bbox(self, cushion_length: bool = False) -> Polygon:
        """Returns a bounding box around the vehicle."""
        # note: lru_cache must be cleared whenever pose changes
        pos = self._state.pose.point
        dims = self._state.dimensions
        length_buffer = self._min_space_cush if cushion_length else 0
        half_len = 0.5 * dims.length + length_buffer
        poly = shapely_box(
            pos.x - 0.5 * dims.width,
            pos.y - half_len,
            pos.x + 0.5 * dims.width,
            pos.y + half_len,
        )
        return shapely_rotate(poly, self._state.pose.heading, use_radians=True)

    class _LaneWindow:
        def __init__(
            self,
            lane: RoadMap.Lane,
            time_left: float,
            ttre: float,
            gap: float,
            lane_coord: RefLinePoint,
            agent_gap: Optional[float],
        ):
            self.lane = lane
            self.time_left = time_left
            self.adj_time_left = time_left  # could eventually be negative
            self.ttre = ttre  # time until we'd get rear-ended
            self.gap = gap  # just the gap ahead (in meters)
            self.lane_coord = lane_coord
            self.agent_gap = agent_gap

        @cached_property
        def width(self) -> float:
            """The width of this lane at its lane_coord."""
            return self.lane.width_at_offset(self.lane_coord.s)[0]

        @cached_property
        def radius(self) -> float:
            """The radius of curvature of this lane at its lane_coord."""
            return self.lane.curvature_radius_at_offset(
                self.lane_coord.s, lookahead=max(math.ceil(2 * self.width), 2)
            )

        @lru_cache(maxsize=4)
        def _angle_scale(self, to_index: int, theta: float = math.pi / 6) -> float:
            # we need to correct for not going straight across.
            # other things being equal, we target ~30 degrees (sin(30)=.5) on average.
            if abs(self.radius) > 1e5 or self.radius == 0:
                return 1.0 / math.sin(theta)
            # here we correct for the local road curvature (which affects how far we must travel)...
            T = self.radius / self.width
            if to_index > self.lane.index:
                se = T * (T - 1)
                return math.sqrt(
                    2 * (se + 0.5 - se * math.cos(1 / (math.tan(theta) * (T - 1))))
                )
            se = T * (T + 1)
            return math.sqrt(
                2 * (se + 0.5 - se * math.cos(1 / (math.tan(theta) * (T + 1))))
            )

        def crossing_time_at_speed(
            self, to_index: int, speed: float, acc: float = 0.0
        ) -> float:
            """Returns how long it would take to cross from this lane to
            the lane indexed by to_index given our current speed and acceleration."""
            angle_scale = self._angle_scale(to_index)
            return time_to_cover(angle_scale * self.width, speed, acc)

        @lru_cache(maxsize=8)
        def exit_time(self, speed: float, to_index: int, acc: float = 0.0) -> float:
            """Returns how long it would take to drive into the to_index lane
            from this lane given our current speed and acceleration."""
            ct = self.crossing_time_at_speed(to_index, speed, acc)
            t = self.lane_coord.t
            pm = (-1 if to_index >= self.lane.index else 1) * np.sign(t)
            angle_scale = self._angle_scale(to_index)
            return 0.5 * ct + pm * time_to_cover(angle_scale * abs(t), speed, acc)

    def _connected_route_lanes(
        self,
        start_lane: RoadMap.Lane,
        end_lane: RoadMap.Lane,
        route_lens: Dict[Tuple[str, int], float],
        r_ind: int,
    ) -> bool:
        r_ind += 1
        for ogl in start_lane.outgoing_lanes:
            ogl = ogl.composite_lane
            if ogl == end_lane or (
                (ogl.lane_id, r_ind) in route_lens
                and self._connected_route_lanes(ogl, end_lane, route_lens, r_ind)
            ):
                return True
        return False

    def _compute_lane_window(self, lane: RoadMap.Lane, next_route_road: RoadMap.Road):
        lane_coord = lane.to_lane_coord(self._state.pose.point)
        my_offset = lane_coord.s
        my_route_lens = self._owner._route_lane_lengths[self._route_id]
        path_len = my_route_lens.get((lane.lane_id, self._route_ind), lane.length)
        path_len -= my_offset
        lane_time_left = path_len / self.speed

        lane_ttc = lane_ttre = lane_gap = math.inf
        my_front_offset = my_offset + 0.5 * self._state.dimensions.length
        my_back_offset = my_offset - 0.5 * self._state.dimensions.length
        agent_gap = None
        # TODO:  could instead do this via a lane search (forward and backward from my current pos) until we hit something in each lane path.
        # TODO:     instead of veh_id -> nearest_lane cache, can keep lane -> (veh_id, offset) cache
        # TODO:     only search along my (known) route (TAI: dynamic routing)
        # TODO:     could abort that search after window gets "wide enough that it doesn't matter" (horizon)
        # TODO:  whether this is faster will depend on the number of vehicles in the sim relative to the number of lanes locally
        # TODO:     pre-compute nearest_lane and offset in that lane for each vehicle since every one (that we control) needs that anyway (assumes we control most of them)
        # TODO:         then here, my offset in that lane, window is easy if it's b/w  veicles.
        # TODO:             if no vehicles "in front" in that lane, route projection
        # TODO:             if we're going cutoffs it's probably necessary to deal with lane boundaries behind us too??
        for ovs in self._all_vehicle_states:
            if ovs.vehicle_id == self._state.vehicle_id:
                continue
            ov_lane = self._owner._nearest_lanes_cache[ovs.vehicle_id]
            if not ov_lane:
                continue

            ov_lane = ov_lane.composite_lane
            if ov_lane == lane:
                ov_offset = self._owner._cached_lane_offset(ovs, lane)
            else:
                for rind in range(self._route_ind + 1, len(self._route)):
                    ov_route_len = my_route_lens.get((ov_lane.lane_id, self._route_ind))
                    if ov_route_len:
                        break
                else:
                    continue
                if ov_route_len < path_len:
                    connected = self._connected_route_lanes(
                        lane, ov_lane, my_route_lens, self._route_ind
                    )
                else:
                    connected = self._connected_route_lanes(
                        ov_lane, lane, my_route_lens, self._route_ind
                    )
                if not connected:
                    continue
                ov_offset = self._owner._cached_lane_offset(ovs, ov_lane)
                ov_offset += my_offset + (path_len - ov_route_len)

            ov_front_offset = ov_offset + 0.5 * ovs.dimensions.length
            ov_back_offset = ov_offset - 0.5 * ovs.dimensions.length

            my_speed, my_acc = self._lane_speed[lane.index]
            speed_delta = my_speed - ovs.speed
            acc_delta = my_acc
            if ovs.linear_acceleration is not None:
                acc_delta -= np.linalg.norm(ovs.linear_acceleration)

            if my_offset <= ov_offset:
                ov_front_gap = max(ov_back_offset - my_front_offset, 0)
                if ov_front_gap < lane_gap:
                    lane_gap = ov_front_gap

                if lane_ttc > 0:
                    ntc = max(ov_front_gap - self._min_space_cush, 0)
                    ov_ttc = max(time_to_cover(ntc, speed_delta, acc_delta), 0)
                    if ov_ttc < lane_ttc:
                        lane_ttc = ov_ttc

            elif lane_ttre > 0:
                ov_back_gap = max(
                    my_back_offset - ov_front_offset - self._min_space_cush, 0
                )
                ov_ttre = time_to_cover(ov_back_gap, -speed_delta, -acc_delta)
                if ov_ttre < lane_ttre:
                    lane_ttre = ov_ttre
                if ovs.role == ActorRole.EgoAgent and (
                    agent_gap is None or (ov_back_gap > 0 and ov_back_gap < agent_gap)
                ):
                    agent_gap = ov_back_gap

            if lane_ttc == 0 and lane_ttre == 0:
                assert lane_gap <= self._min_space_cush, f"{lane_gap}"
                break

        self._lane_windows[lane.index] = _TrafficActor._LaneWindow(
            lane,
            min(lane_time_left, lane_ttc),
            lane_ttre,
            lane_gap,
            lane_coord,
            agent_gap,
        )

    def _compute_lane_windows(self):
        self._lane_windows = dict()
        for road, next_route_road in zip(self._route, self._route[1:]):
            if self.road == road:
                break
        else:
            next_route_road = None
        for lane in self.road.lanes:
            self._compute_lane_window(lane, next_route_road)
        for index, lw in self._lane_windows.items():
            lw.adj_time_left -= self._crossing_time_into(index)[0]

    def _crossing_time_into(self, target_idx: int) -> Tuple[float, bool]:
        my_idx = self._lane.index
        if my_idx == target_idx:
            return 0.0, True
        min_idx = min(target_idx, my_idx + 1)
        max_idx = max(target_idx + 1, my_idx)
        cross_time = self._lane_windows[my_idx].exit_time(
            self.speed, target_idx, self.acceleration
        )
        for i in range(min_idx, max_idx):
            lw = self._lane_windows[i]
            lct = lw.crossing_time_at_speed(target_idx, self.speed, self.acceleration)
            if i == target_idx:
                lct *= 0.5
            cross_time += lct
        # note: we *could* be more clever and use cross_time for each lane separately
        # to try to thread our way through the gaps independently... nah.
        for i in range(min_idx, max_idx):
            lw = self._lane_windows[i]
            if min(lw.time_left, lw.ttre) <= cross_time:
                return cross_time, False
        return cross_time, True

    def _should_cutin(self, lw: _LaneWindow) -> bool:
        if lw.lane.index == self._lane.index:
            return False
        min_gap = self._target_cutin_gap / self._aggressiveness
        max_gap = self._target_cutin_gap + 2
        if (
            min_gap < lw.agent_gap < max_gap
            and self._crossing_time_into(lw.lane.index)[1]
        ):
            return random.random() < self._cutin_prob
        return False

    def _pick_lane(self, dt: float):
        # TODO:  only use lane windows if there's a *chance* we're changing lanes
        self._compute_lane_windows()
        my_idx = self._lane.index
        self._lane_win = my_lw = self._lane_windows[my_idx]
        best_lw = self._lane_windows[my_idx]
        idx = my_idx
        while 0 <= idx < len(self._lane_windows):
            if not self._crossing_time_into(idx)[1]:
                break
            lw = self._lane_windows[idx]
            if (
                lw.lane == self._dest_lane
                and lw.lane_coord.s + lw.gap >= self._dest_offset
            ):
                best_lw = lw
                break
            if (
                self._cutting_into
                and self._crossing_time_into(self._cutting_into.lane.index)[1]
            ):
                best_lw = self._cutting_into
                if self._cutting_into.lane != self._lane:
                    break
                self._in_front_after_cutin_secs += dt
                if self._in_front_secs < self._cutin_hold_secs:
                    break
            self._cutting_into = None
            self._in_front_secs = 0
            if lw.agent_gap and self._should_cutin(lw):
                best_lw = lw
                self._cutting_into = lw
            elif lw.adj_time_left > best_lw.adj_time_left or (
                lw.adj_time_left == best_lw.adj_time_left
                and (
                    (lw.lane == self._dest_lane and self._offset < self._dest_offset)
                    or (lw.ttre > best_lw.ttre and idx < best_lw.lane.index)
                )
            ):
                best_lw = lw
            idx += 1
            idx %= len(self._lane_windows)
            if idx == my_idx:
                break
        self._target_lane_win = best_lw

    def _compute_lane_speeds(self):
        def _get_radius(lane: RoadMap.Lane) -> float:
            l_offset = self._owner._cached_lane_offset(self._state, lane)
            l_width, _ = lane.width_at_offset(l_offset)
            return lane.curvature_radius_at_offset(
                l_offset, lookahead=max(math.ceil(2 * l_width), 2)
            )

        self._lane_speed = dict()
        my_radius = _get_radius(self._lane)
        for l in self._lane.road.lanes:
            ratio = 1.0
            l_radius = _get_radius(l)
            if abs(my_radius) < 1e5 and abs(l_radius) < 1e5:
                ratio = l_radius / my_radius
                if ratio < 0:
                    ratio = 1.0
            self._lane_speed[l.index] = (ratio * self.speed, ratio * self.acceleration)

    def _slow_for_curves(self):
        # XXX:  this may be too expensive.  if so, we'll need to precompute curvy spots for routes
        lookahead = math.ceil(1 + math.log(self._target_speed))
        radius = self._lane.curvature_radius_at_offset(self._offset, lookahead)
        # pi/2 radian right turn == 6 m/s with radius 10.5 m
        # TODO:  also depends on vehicle type (traction, length, etc.)
        self._target_speed = min(abs(radius) * 0.5714, self._target_speed)

    def _check_speed(self):
        target_lane = self._target_lane_win.lane
        if target_lane.speed_limit is None:
            self._target_speed = self.speed
            self._slow_for_curves()
            return
        self._target_speed = target_lane.speed_limit
        self._target_speed *= self._speed_factor
        self._slow_for_curves()
        max_speed = float(self._vtype.get("maxSpeed", 55.55))
        if self._target_speed >= max_speed:
            self._target_speed = max_speed

    def _angle_to_lane(self, dt: float) -> float:
        my_heading = self._state.pose.heading
        look_ahead = max(dt * self.speed, 2)
        proj_pt = self._state.pose.position[:2] + look_ahead * radians_to_vec(
            my_heading
        )
        proj_pt = Point(*proj_pt)

        target_lane = self._target_lane_win.lane
        lane_coord = target_lane.to_lane_coord(proj_pt)
        lat_err = lane_coord.t

        target_vec = target_lane.vector_at_offset(lane_coord.s)
        target_heading = vec_to_radians(target_vec[:2])
        heading_delta = min_angles_difference_signed(target_heading, my_heading)

        # Here we may also want to take into account speed, accel, inertia, etc.
        # and maybe even self._aggressiveness...

        # TODO: use float(self._vtype.get("sigma", 0.5)) to add some random variation

        # magic numbers here were just what looked reasonable in limited testing
        return 3.75 * heading_delta - 1.25 * lat_err

    def _compute_acceleration(self, dt: float) -> float:
        emergency_decl = float(self._vtype.get("emergencyDecel", 4.5))
        assert emergency_decl >= 0.0
        time_cush = max(
            min(
                self._target_lane_win.time_left,
                self._target_lane_win.gap / self.speed,
                self._lane_win.time_left,
                self._lane_win.gap / self.speed,
            ),
            0,
        )
        min_time_cush = float(self._vtype.get("tau", 1.0))
        if time_cush < min_time_cush:
            if self.speed > 0:
                severity = 3 * (min_time_cush - time_cush) / min_time_cush
                return -emergency_decl * np.clip(severity, 0, 1.0)
            return 0

        space_cush = max(min(self._target_lane_win.gap, self._lane_win.gap), 0)
        if space_cush < self._min_space_cush:
            if self.speed > 0:
                severity = (
                    2 * (self._min_space_cush - space_cush) / self._min_space_cush
                )
                return -emergency_decl * np.clip(severity, 0, 1.0)
            return 0

        my_speed, my_acc = self._lane_speed[self._target_lane_win.lane.index]

        P = 0.0060 * (self._target_speed - my_speed)
        I = 0  # 0.0040 * (target_lane_offset - my_target_lane_offset)
        D = -0.0010 * my_acc
        PID = (P + I + D) / dt
        PID = np.clip(PID, -1.0, 1.0)

        # TODO: use float(self._vtype.get("sigma", 0.5)) to add some random variation

        if PID > 0:
            max_accel = float(self._vtype.get("accel", 2.6))
            assert max_accel >= 0.0
            return PID * max_accel

        max_decel = float(self._vtype.get("decel", 4.5))
        assert max_decel >= 0.0
        return PID * max_decel

    def compute_next_state(self, dt: float, all_vehicle_states: Sequence[VehicleState]):
        """Pre-computes the next state for this traffic actor."""
        self._all_vehicle_states = all_vehicle_states
        self._compute_lane_speeds()  # TODO: curvature only needed if we might change lanes

        self._pick_lane(dt)
        self._check_speed()

        angular_velocity = self._angle_to_lane(dt)
        acceleration = self._compute_acceleration(dt)

        target_heading = self._state.pose.heading + angular_velocity * dt
        target_heading %= 2 * math.pi
        heading_vec = radians_to_vec(target_heading)
        self._next_linear_acceleration = dt * acceleration * heading_vec
        self._next_speed = self._state.speed + acceleration * dt
        dpos = heading_vec * self.speed * dt
        target_pos = self._state.pose.position + np.append(dpos, 0.0)
        self._next_pose = Pose.from_center(target_pos, Heading(target_heading))

    def step(self, dt: float):
        """Updates to the pre-computed next state for this traffic actor."""
        self._state.pose = self._next_pose
        self._state.speed = self._next_speed
        self._state.linear_acceleration = self._next_linear_acceleration
        prev_road_id = self._lane.road.road_id
        self.bbox.cache_clear()

        # if there's more than one lane near us (like in a junction) pick one that's in our route
        nls = self._owner.road_map.nearest_lanes(
            self._next_pose.point,
            radius=self._state.dimensions.length,
            include_junctions=True,
        )
        self._lane = None
        best_d = None
        self._off_route = True
        for nl, d in nls:
            if nl.road.road_id in self._route:
                self._lane = nl
                self._off_route = False
                break
            if best_d is None or d < best_d:
                best_d = d
                self._lane = nl
        # TODO:  eventually just remove vehicles that drive off road?
        assert self._lane, f"actor {self.actor_id} out-of-lane:  {self._next_pose}"
        self._lane = self._lane.composite_lane

        road_id = self._lane.road.road_id
        if road_id != prev_road_id:
            self._route_ind += 1

        self._offset = self._lane.offset_along_lane(self._next_pose.point)
        if self._lane == self._dest_lane and self._offset >= self._dest_offset:
            if self._owner._endless_traffic:
                self._reroute()
            else:
                self._done_with_route = True

    def _reroute(self):
        if self._route[0] in {oid.road_id for oid in self._lane.road.outgoing_roads}:
            self._route_ind = -1
            self._logger.debug(
                f"{self.actor_id} will loop around to beginning of its route"
            )
            return
        self._logger.info(f"{self.actor_id} teleporting back to beginning of its route")
        self._lane, self._offset = self._resolve_flow_pos(
            self._flow, "depart", self._state.dimensions
        )
        position = self._lane.from_lane_coord(RefLinePoint(s=self._offset))
        heading = vec_to_radians(self._lane.vector_at_offset(self._offset)[:2])
        self._state.pose = Pose.from_center(position, Heading(heading))
        self._state.speed = self._resolve_flow_speed(self._flow)
        self._state.linear_acceleration = np.array((0.0, 0.0, 0.0))
        self._route_ind = 0