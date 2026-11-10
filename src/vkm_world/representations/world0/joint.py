"""Axial World-0 branch with coupled force balance and linear Maxwell memory.

The input explicitly declares a graph stiffness hypothesis and applied forces.
Norton exponent one, scalar convergence and constant external forces are the
scope of this representation. Its convergence is not surface displacement.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from scipy.sparse import coo_matrix, diags
from scipy.sparse.linalg import spsolve


INPUT_SCHEMA = "vkm.world0.joint/1"
TRAJECTORY_SCHEMA = "vkm.world0.joint-trajectory/1"
CHECKPOINT_SCHEMA = "vkm.world0.joint-checkpoint/1"
EVENTS = {"excavate", "backfill"}


class ConvergenceError(RuntimeError):
    """The candidate step did not meet the declared numerical checks."""


def _number(value: float, name: str, *, minimum: float = 0, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if value < minimum or (strict and value == minimum):
        raise ValueError(f"{name} outside the supported interval")
    return float(value)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identifiers must be nonempty strings")
    return value


@dataclass(frozen=True)
class Event:
    time_s: float
    node: int
    kind: str


@dataclass(frozen=True)
class State:
    """Committed primary state; derived forces are reconstructed on use."""

    time_s: float
    q: np.ndarray
    memory: np.ndarray
    excavated: np.ndarray
    fill_reference: np.ndarray  # NaN marks a fill that has not been born.
    next_event: int
    phase: str


class JointSolver:
    def __init__(self, data: dict):
        if data["schema"] != INPUT_SCHEMA:
            raise ValueError("unsupported joint world schema")
        self.world_id = _identifier(data["world_id"])
        self.representation_id = _identifier(data["representation_id"])
        self.end_time = _number(data["end_time_s"], "end_time_s", strict=True)
        self.max_dt = _number(data["max_dt_s"], "max_dt_s", strict=True)
        nodes = data["nodes"]
        self.node_ids = [_identifier(node["id"]) for node in nodes]
        node_index = {name: i for i, name in enumerate(self.node_ids)}
        n = len(nodes)
        if not n or len(node_index) != n:
            raise ValueError("nodes must be nonempty and have unique identifiers")

        def column(name: str, *, strict: bool = False) -> np.ndarray:
            return np.array([_number(node[name], name, strict=strict) for node in nodes])

        self.area = column("area_m2", strict=True)
        self.height = column("height_m", strict=True)
        self.young = column("young_pa", strict=True)
        self.viscosity = column("viscosity_pa_s", strict=True)
        self.chamber_fraction = column("chamber_fraction")
        if np.any(self.chamber_fraction >= 1):
            raise ValueError("excavation must retain a positive load-bearing area")
        self.fill_k = column("fill_stiffness_n_m")
        self.fill_gap = column("fill_gap_m")
        self.load = column("load_n")
        self.initial_memory = column("initial_memory_m")
        self.force_atol = max(1e-6, 1e-10 * np.max(self.load))

        def index(name: str) -> int:
            if name not in node_index:
                raise ValueError(f"unknown node {name!r}")
            return node_index[name]

        edge_pairs = set()
        edge_rows = []
        for edge in data["edges"]:
            pair = tuple(sorted((index(edge["a"]), index(edge["b"]))))
            if pair[0] == pair[1] or pair in edge_pairs:
                raise ValueError("mechanical edges must be distinct undirected pairs")
            edge_pairs.add(pair)
            edge_rows.append((*pair, _number(edge["stiffness_n_m"], "stiffness_n_m", strict=True)))
        self.edge_a = np.array([row[0] for row in edge_rows], dtype=int)
        self.edge_b = np.array([row[1] for row in edge_rows], dtype=int)
        self.edge_k = np.array([row[2] for row in edge_rows])
        self.k = coo_matrix(
            (
                np.tile(self.edge_k, 4) * np.repeat([1, 1, -1, -1], len(edge_rows)),
                (
                    np.concatenate((self.edge_a, self.edge_b, self.edge_a, self.edge_b)),
                    np.concatenate((self.edge_a, self.edge_b, self.edge_b, self.edge_a)),
                ),
            ),
            shape=(n, n),
        ).tocsr()

        self.events = sorted(
            (
                Event(_number(e["time_s"], "event.time_s", strict=True), index(e["node"]), e["kind"])
                for e in data["events"]
            ),
            key=lambda e: (e.time_s, e.node),
        )
        last = {}
        for event in self.events:
            prior = last.get(event.node)
            if event.kind not in EVENTS or event.time_s > self.end_time:
                raise ValueError("invalid event kind or event after end time")
            if event.kind == "excavate":
                valid = prior is None and self.chamber_fraction[event.node] > 0
            else:
                valid = (
                    prior is not None
                    and prior.kind == "excavate"
                    and prior.time_s < event.time_s
                    and self.fill_k[event.node] > 0
                )
            if not valid:
                raise ValueError("event violates excavation and backfill lifecycle")
            last[event.node] = event

    def _stiffness(self, excavated: np.ndarray) -> np.ndarray:
        return self.young * self.area * (1 - self.chamber_fraction * excavated) / self.height

    @staticmethod
    def _freeze(array: np.ndarray) -> np.ndarray:
        array = np.array(array, copy=True)
        array.flags.writeable = False
        return array

    def _state(self, time, q, memory, excavated, fill_reference, next_event, phase) -> State:
        return State(
            time, self._freeze(q), self._freeze(memory), self._freeze(excavated),
            self._freeze(fill_reference), next_event, phase,
        )

    def _fill_opening(self, q: np.ndarray, reference: np.ndarray) -> np.ndarray:
        return np.maximum(0, q - np.where(np.isnan(reference), np.inf, reference))

    def _solve(
        self,
        time: float,
        dt: float,
        q: np.ndarray,
        memory: np.ndarray,
        excavated: np.ndarray,
        reference: np.ndarray,
        next_event: int,
        phase: str,
    ) -> State:
        elastic = self._stiffness(excavated)
        gamma = dt * self.young / self.viscosity
        eff = elastic / (1 + gamma)
        matrix = self.k + diags(eff)
        for _ in range(40):
            coupling = self.k @ q
            closing = self._fill_opening(q, reference)
            force = eff * (q - memory)
            residual = coupling + force + self.fill_k * closing - self.load
            if np.linalg.norm(residual, np.inf) <= self.force_atol:
                new_memory = memory + gamma * force / elastic
                actual = coupling + elastic * (q - new_memory) + self.fill_k * closing - self.load
                if not np.all(np.isfinite(new_memory)) or np.linalg.norm(actual, np.inf) > 4 * self.force_atol:
                    raise ConvergenceError(f"committed force or memory check failed at {time:g} s")
                return self._state(time, q, new_memory, excavated, reference, next_event, phase)

            active = closing > 0
            direction = spsolve(matrix + diags(self.fill_k * active), -residual)
            work = float(residual @ direction)
            curvature = float(direction @ (matrix @ direction))
            if not math.isfinite(work) or work >= 0:
                break
            for power in range(24):
                step = 2.0 ** -power
                trial = q + step * direction
                trial_closing = self._fill_opening(trial, reference)
                # Stable exact potential increment for the quadratic and unilateral terms.
                fill_delta = (trial_closing - closing) * (trial_closing + closing) / 2
                fill_remainder = self.fill_k @ (fill_delta - step * closing * direction)
                delta = step * work + 0.5 * step**2 * curvature + fill_remainder
                if delta <= 1e-4 * step * work:
                    q = trial
                    break
            else:
                break
        raise ConvergenceError(f"coupled equilibrium did not converge at {time:g} s, phase {phase}")

    def initial(self) -> State:
        zeros = np.zeros(len(self.node_ids))
        return self._solve(
            0, 0, zeros, self.initial_memory, zeros.astype(bool), np.full_like(zeros, np.nan), 0, "initial"
        )

    def forces(self, state: State) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        return (
            self._stiffness(state.excavated) * (state.q - state.memory),
            self.fill_k * self._fill_opening(state.q, state.fill_reference),
            self.k @ state.q,
        )

    def energy(self, state: State) -> float:
        solid, fill, _ = self.forces(state)
        return float(0.5 * (
            solid @ (state.q - state.memory)
            + fill @ self._fill_opening(state.q, state.fill_reference)
            + self.edge_k @ (state.q[self.edge_a] - state.q[self.edge_b]) ** 2
        ))

    def advance(self, state: State, target: float, phase: str = "step") -> State:
        dt = target - state.time_s
        if not math.isfinite(dt) or dt <= 0 or target > self.end_time:
            raise ValueError("step must strictly advance time")
        if state.next_event < len(self.events):
            next_time = self.events[state.next_event].time_s
            if target > next_time or (target == next_time and phase != "pre_event"):
                raise ValueError("step would cross a scheduled event")
        return self._solve(
            target, dt, state.q, state.memory, state.excavated, state.fill_reference, state.next_event, phase
        )

    def apply_events(self, pre: State) -> tuple[State, float]:
        if pre.next_event >= len(self.events) or self.events[pre.next_event].time_s != pre.time_s:
            raise ValueError("no scheduled event at current time")
        excavated = pre.excavated.copy()
        reference = pre.fill_reference.copy()
        removed = 0.0
        i = pre.next_event
        k = self._stiffness(excavated)
        while i < len(self.events) and self.events[i].time_s == pre.time_s:
            event = self.events[i]
            j = event.node
            if event.kind == "excavate":
                excavated[j] = True
                removed += 0.5 * k[j] * self.chamber_fraction[j] * (pre.q[j] - pre.memory[j]) ** 2
            else:
                reference[j] = pre.q[j] + self.fill_gap[j]
            i += 1
        post = self._solve(pre.time_s, 0, pre.q, pre.memory, excavated, reference, i, "post_event")
        return post, removed

    def snapshot(self, state: State, prior: State | None = None, removed: float = 0.0) -> dict:
        solid, fill, coupling = self.forces(state)
        energy = self.energy(state)
        result = {
            "time_s": state.time_s,
            "phase": state.phase,
            "convergence_m": state.q.tolist(),
            "creep_memory_m": state.memory.tolist(),
            "pillar_force_n": solid.tolist(),
            "fill_force_n": fill.tolist(),
            "fill_reference_m": [None if math.isnan(x) else x for x in state.fill_reference],
            "coupling_force_n": coupling.tolist(),
            "edge_force_n": (self.edge_k * (state.q[self.edge_a] - state.q[self.edge_b])).tolist(),
            "force_residual_max_n": float(np.max(np.abs(solid + fill + coupling - self.load))),
            "excavated": state.excavated.tolist(),
            "backfilled": np.isfinite(state.fill_reference).tolist(),
            "next_event": state.next_event,
            "elastic_energy_j": energy,
            "creep_dissipation_j": 0.0,
            "event_removed_energy_j": removed,
        }
        if prior is not None:
            work = float(self.load @ (state.q - prior.q))
            dissipation = float(solid @ (state.memory - prior.memory))
            result["creep_dissipation_j"] = dissipation
            result["external_work_j"] = work
            result["energy_balance_remainder_j"] = work - (energy - self.energy(prior)) - dissipation - removed
            energy_floor = 1e-9 * max(abs(work), energy, self.energy(prior), 1.0)
            if min(dissipation, result["energy_balance_remainder_j"]) < -energy_floor:
                raise ConvergenceError(f"negative dissipation or energy remainder at {state.time_s:g} s")
        return result

    def restore(self, snapshot: dict) -> State:
        """Restore immutable state and reject inconsistent event history or force balance."""
        q = np.asarray(snapshot["convergence_m"], dtype=float)
        memory = np.asarray(snapshot["creep_memory_m"], dtype=float)
        reference = np.asarray(snapshot["fill_reference_m"], dtype=float)
        excavated = np.asarray(snapshot["excavated"])
        count = snapshot["next_event"]
        time = _number(snapshot["time_s"], "checkpoint time")
        phase = snapshot["phase"]
        if (
            any(arr.shape != self.load.shape for arr in (q, memory, reference, excavated))
            or not np.all(np.isfinite(q))
            or not np.all(np.isfinite(memory))
            or np.any(np.isinf(reference))
            or excavated.dtype != np.dtype(bool)
            or type(count) is not int
            or not 0 <= count <= len(self.events)
            or time > self.end_time
            or phase not in {"initial", "step", "pre_event", "post_event"}
        ):
            raise ValueError("invalid checkpoint state")
        has_event = any(e.time_s == time for e in self.events)
        if (phase == "initial") != (time == 0) or (phase.endswith("event") != has_event):
            raise ValueError("checkpoint phase disagrees with the event calendar")
        processed = sum(e.time_s < time or (e.time_s == time and phase == "post_event") for e in self.events)
        if count != processed:
            raise ValueError("checkpoint event cursor disagrees with phase and time")
        mined = np.zeros_like(excavated)
        filled = np.zeros_like(excavated)
        for event in self.events[:count]:
            (mined if event.kind == "excavate" else filled)[event.node] = True
        if not np.array_equal(excavated, mined) or not np.array_equal(np.isfinite(reference), filled):
            raise ValueError("checkpoint activation disagrees with event history")
        state = self._state(time, q, memory, excavated, reference, count, phase)
        solid, fill, coupling = self.forces(state)
        if np.linalg.norm(solid + fill + coupling - self.load, np.inf) > 4 * self.force_atol:
            raise ValueError("checkpoint does not balance the applied force")
        return state

    def run(self, state: State | None = None, *, stop_after_event: int | None = None) -> tuple[list[dict], State]:
        state = self.initial() if state is None else state
        snapshots = [self.snapshot(state)]
        while True:
            event_time = (
                self.events[state.next_event].time_s if state.next_event < len(self.events) else np.inf
            )
            if event_time == state.time_s:
                post, removed = self.apply_events(state)
                snapshots.append(self.snapshot(post, state, removed))
                state = post
                if stop_after_event is not None and state.next_event >= stop_after_event:
                    break
                continue
            if state.time_s >= self.end_time:
                break
            target = min(state.time_s + self.max_dt, self.end_time, event_time)
            if target <= state.time_s:
                raise ValueError("maximum step is below the time coordinate precision")
            for rejected in range(9):
                phase = "pre_event" if target == event_time else "step"
                try:
                    advanced = self.advance(state, target, phase)
                    snapshot = self.snapshot(advanced, state)
                except ConvergenceError:
                    if rejected == 8:
                        raise
                    target = state.time_s + (target - state.time_s) / 2
                    if target <= state.time_s:
                        raise
                else:
                    snapshot["rejected_trials"] = rejected
                    snapshots.append(snapshot)
                    state = advanced
                    break
        return snapshots, state


def _read(path: Path) -> tuple[dict, str]:
    raw = path.read_bytes()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _write(path: Path, payload: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, allow_nan=False, separators=(",", ":"))
        stream.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("world", type=Path)
    parser.add_argument("trajectory", type=Path)
    parser.add_argument("--restart", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--stop-after-event", type=int)
    args = parser.parse_args(argv)
    data, digest = _read(args.world)
    solver = JointSolver(data)
    state = None
    if args.restart is not None:
        checkpoint, _ = _read(args.restart)
        if checkpoint["schema"] != CHECKPOINT_SCHEMA or checkpoint["input_sha256"] != digest:
            raise ValueError("checkpoint does not match world input bytes")
        state = solver.restore(checkpoint["state"])
    if args.stop_after_event is not None and not 0 < args.stop_after_event <= len(solver.events):
        raise ValueError("stop-after-event is a 1-based event cursor")
    snapshots, state = solver.run(state, stop_after_event=args.stop_after_event)
    identity = {
        "world_id": solver.world_id,
        "representation_id": solver.representation_id,
        "input_sha256": digest,
    }
    _write(args.trajectory, {
        "schema": TRAJECTORY_SCHEMA, **identity, "node_ids": solver.node_ids,
        "force_tolerance_n": 4 * solver.force_atol, "snapshots": snapshots,
        "edge_nodes": [[solver.node_ids[a], solver.node_ids[b]] for a, b in zip(solver.edge_a, solver.edge_b)],
    })
    if args.checkpoint is not None:
        _write(args.checkpoint, {"schema": CHECKPOINT_SCHEMA, **identity, "state": solver.snapshot(state)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
