"""
Rule engine core for the Microgrid Energy Manager.

Ported from the prototype's client-side JS (runEngine / buildGraph /
detectCycle / evalCondition) into pure, testable Python with no Flask
or DB dependency — app.py wires this to HTTP/WebSocket and database.py
persists it.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Optional

MAX_ITER = 20

# ---------------------------------------------------------------------------
# Domain model
# ---------------------------------------------------------------------------

SENSOR_DEFS = [
    {"key": "Solar_Output", "label": "Solar Output", "unit": "kW", "min": 0, "max": 10},
    {"key": "Grid_Tariff_Price", "label": "Grid Tariff", "unit": "$/kWh", "min": 0.10, "max": 0.60},
    {"key": "Battery_Level", "label": "Battery Level", "unit": "%", "min": 0, "max": 100},
    {"key": "Building_Temp", "label": "Building Temp", "unit": "°C", "min": 15, "max": 40},
]
ACTUATOR_DEFS = [
    {"key": "HVAC_System", "label": "HVAC System", "values": ["OFF", "ECO", "MAX"]},
    {"key": "EV_Charger", "label": "EV Charger", "values": ["ON", "OFF"]},
    {"key": "Battery_Discharge", "label": "Battery Discharge", "values": ["ON", "OFF"]},
]
ALL_DEVICE_KEYS = [s["key"] for s in SENSOR_DEFS] + [a["key"] for a in ACTUATOR_DEFS]
DEVICE_LABEL = {d["key"]: d["label"] for d in SENSOR_DEFS + ACTUATOR_DEFS}
SENSOR_KEYS = {s["key"] for s in SENSOR_DEFS}
ACTUATOR_KEYS = {a["key"] for a in ACTUATOR_DEFS}

DEFAULT_STATE = {
    "Solar_Output": 3,
    "Grid_Tariff_Price": 0.25,
    "Battery_Level": 62,
    "Building_Temp": 24,
    "HVAC_System": "OFF",
    "EV_Charger": "OFF",
    "Battery_Discharge": "ON",
}

DEFAULT_RULES = [
    {"id": "R1", "name": "Solar EV Charging",
     "condition": {"input": "Solar_Output", "operator": ">", "value": 7},
     "action": {"device": "EV_Charger", "value": "ON"}, "priority": 2, "enabled": True},
    {"id": "R2", "name": "Preserve Battery",
     "condition": {"input": "EV_Charger", "operator": "==", "value": "ON"},
     "action": {"device": "Battery_Discharge", "value": "OFF"}, "priority": 4, "enabled": True},
    {"id": "R3", "name": "High Temperature Cooling",
     "condition": {"input": "Building_Temp", "operator": ">", "value": 30},
     "action": {"device": "HVAC_System", "value": "MAX"}, "priority": 3, "enabled": True},
    {"id": "R4", "name": "Peak Tariff Saving",
     "condition": {"input": "Grid_Tariff_Price", "operator": ">", "value": 0.30},
     "action": {"device": "HVAC_System", "value": "ECO"}, "priority": 8, "enabled": True},
    {"id": "R5", "name": "Low Battery Protection",
     "condition": {"input": "Battery_Level", "operator": "<", "value": 20},
     "action": {"device": "Battery_Discharge", "value": "OFF"}, "priority": 10, "enabled": True},
]
# NOTE: the original prototype shipped a 6th rule ("Solar Feedback (demo)":
# Battery_Discharge==ON -> Solar_Output=10) permanently active by default.
# Combined with R1 (Solar_Output -> EV_Charger) and R2 (EV_Charger ->
# Battery_Discharge) it closes a structural loop back to Solar_Output. Since
# cycle detection scans the whole active-rule graph, leaving that rule
# enabled at all times meant EVERY future rule addition — including safe
# ones like the "Battery Cutoff" live-injection demo — got rejected as a
# false-positive cycle. It's used instead as the live payload for the
# Scenario 3 "cycle attack" demo (see app.py), so it still demonstrates
# rejection without permanently poisoning the graph.
CYCLE_ATTACK_PAYLOAD = {
    "name": "Solar Feedback (attack)",
    "condition": {"input": "Battery_Discharge", "operator": "==", "value": "ON"},
    "action": {"device": "Solar_Output", "value": 10},
    "priority": 1,
}


class CycleError(Exception):
    """Raised when adding/enabling a rule would create a dependency cycle."""
    def __init__(self, cycle_path: list[str], story: list[str]):
        self.cycle_path = cycle_path
        self.story = story
        super().__init__(" -> ".join(story))


# ---------------------------------------------------------------------------
# Condition evaluation
# ---------------------------------------------------------------------------

def _coerce(left: Any, right: Any) -> tuple[Any, Any]:
    """Compare numerically if both sides parse as numbers, else as strings.
    Mirrors the JS engine's coerce/evalCondition behavior."""
    try:
        nl = float(left)
        nr = float(right)
        return nl, nr
    except (TypeError, ValueError):
        return str(left), str(right)


def eval_condition(cond: dict, st: dict) -> bool:
    left = st.get(cond["input"])
    if left is None:
        return False
    l, r = _coerce(left, cond["value"])
    op = cond["operator"]
    if op == ">":
        return l > r
    if op == "<":
        return l < r
    if op == ">=":
        return l >= r
    if op == "<=":
        return l <= r
    if op == "==":
        return l == r
    if op == "!=":
        return l != r
    return False


# ---------------------------------------------------------------------------
# Graph / cycle detection
# ---------------------------------------------------------------------------
# Node ids: device keys as-is, rules as "RULE:<id>" — same scheme as the JS.

def build_graph(rule_set: list[dict]) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = {k: [] for k in ALL_DEVICE_KEYS}

    def ensure(n: str):
        adj.setdefault(n, [])

    for r in rule_set:
        rn = f"RULE:{r['id']}"
        ensure(rn)
        ensure(r["condition"]["input"])
        ensure(r["action"]["device"])
        adj[r["condition"]["input"]].append(rn)
        adj[rn].append(r["action"]["device"])
    return adj


def detect_cycle(adj: dict[str, list[str]]) -> Optional[list[str]]:
    """DFS with white/gray/black coloring. Returns the cycle path
    (node list, first==last) if a back-edge is found, else None."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {n: WHITE for n in adj}
    cycle_path: list[str] | None = None

    def dfs(node: str, path: list[str]) -> bool:
        nonlocal cycle_path
        color[node] = GRAY
        path.append(node)
        for nxt in adj[node]:
            if color[nxt] == GRAY:
                idx = path.index(nxt)
                cycle_path = path[idx:] + [nxt]
                return True
            if color[nxt] == WHITE:
                if dfs(nxt, path):
                    return True
        path.pop()
        color[node] = BLACK
        return False

    for n in list(adj.keys()):
        if color[n] == WHITE:
            if dfs(n, []):
                return cycle_path
    return None


def humanize_cycle_path(path: list[str]) -> list[str]:
    """Collapse RULE: nodes out for a clean device -> device -> device story."""
    return [DEVICE_LABEL.get(n, n) for n in path if not n.startswith("RULE:")]


def check_cycle_for(rule_set: list[dict], candidate: dict) -> Optional[dict]:
    """Returns {'cycle_path': [...], 'story': [...]} if adding/enabling
    `candidate` would create a cycle among currently-ENABLED rules, else
    None. Does not mutate rule_set.

    Only enabled rules are considered: a disabled rule can't actually fire,
    so it can't contribute to a runtime chaining loop, and including it
    would produce false-positive rejections for unrelated new rules (see
    engine.py's DEFAULT_RULES comment on R6 for a concrete example)."""
    active = [r for r in rule_set if r.get("enabled")]
    test_set = active + [candidate]
    graph = build_graph(test_set)
    cycle = detect_cycle(graph)
    if cycle:
        return {"cycle_path": cycle, "story": humanize_cycle_path(cycle)}
    return None


# ---------------------------------------------------------------------------
# Rule engine — evaluate with chaining + priority-based arbitration
# ---------------------------------------------------------------------------

@dataclass
class EngineResult:
    state: dict
    trace: list[dict] = field(default_factory=list)
    executions: int = 0
    conflicts: int = 0


def run_engine(state: dict, rules: list[dict]) -> EngineResult:
    """Iteratively evaluate rules against `working` state. Each iteration:
      1. find all enabled rules whose condition is currently true
      2. group their claimed actions by target device
      3. for devices with >1 claimant, arbitrate by priority (highest wins)
      4. apply winning actions, log trace entries
      5. repeat until no state change or MAX_ITER reached (chaining)
    This mirrors the prototype's client-side runEngine() exactly, including
    the fixed-point loop that lets one rule's action trigger another rule's
    condition (chaining) within the same engine run.
    """
    working = dict(state)
    trace: list[dict] = []
    exec_count = 0
    conflict_count = 0

    for iteration in range(MAX_ITER):
        active = [r for r in rules if r.get("enabled") and eval_condition(r["condition"], working)]
        if not active:
            break

        claims_by_device: dict[str, list[dict]] = {}
        for r in active:
            claims_by_device.setdefault(r["action"]["device"], []).append(r)

        changed = False
        for device, claims in claims_by_device.items():
            claims.sort(key=lambda c: c["priority"], reverse=True)
            winner = claims[0]
            if len(claims) > 1:
                conflict_count += 1
                trace.append({
                    "type": "arbitration", "device": device,
                    "winner": winner["id"],
                    "losers": [c["id"] for c in claims[1:]],
                    "iter": iteration,
                })
            if str(working.get(device)) != str(winner["action"]["value"]):
                working[device] = winner["action"]["value"]
                changed = True
                exec_count += 1
                trace.append({
                    "type": "trigger", "rule": winner["id"], "device": device,
                    "value": winner["action"]["value"], "iter": iteration,
                })

        if not changed:
            break

    return EngineResult(state=working, trace=trace, executions=exec_count, conflicts=conflict_count)
