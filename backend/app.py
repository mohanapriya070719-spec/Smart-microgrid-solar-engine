"""
Microgrid Energy Manager — backend.

REST API for rules/state/log/stats + a WebSocket channel that pushes
live updates (state changes, rule triggers, arbitrations, cycle
rejections) so the frontend can drop its client-side engine and just
render what the server broadcasts.

Run:
    pip install -r requirements.txt
    python app.py
Serves on http://localhost:5000
"""

import math
import random
import threading
import time

from flask import Flask, g, jsonify, request
from flask_cors import CORS
from flask_socketio import SocketIO

import database as db
import engine
from engine import ALL_DEVICE_KEYS, DEVICE_LABEL, CycleError

app = Flask(__name__)
app.config["SECRET_KEY"] = "microgrid-hackathon-demo"
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# --- simple in-process state for the auto-sim background loop ---------
sim_lock = threading.Lock()
sim_state = {"mode": "auto", "tick": 0, "running": False, "cycle_path": None}


# ---------------------------------------------------------------------------
# DB connection per request
# ---------------------------------------------------------------------------

def get_db():
    if "db" not in g:
        g.db = db.get_connection()
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# ---------------------------------------------------------------------------
# Shared helpers — run the engine, persist results, broadcast over WS
# ---------------------------------------------------------------------------

def broadcast_log(conn, type_: str, msg: str):
    entry = db.log_event(conn, type_, msg)
    socketio.emit("log_event", entry)
    return entry


def execute_engine_and_broadcast(conn, reason: str = "manual"):
    """Runs the engine against current DB state/rules, persists the
    resulting state + counters, and emits WebSocket events for every
    trace step so the frontend graph/trace panel can animate live."""
    state = db.get_state(conn)
    rules = db.get_all_rules(conn)
    result = engine.run_engine(state, rules)

    db.set_state(conn, result.state)
    db.bump_counters(conn, executions=result.executions, conflicts=result.conflicts)

    for step in result.trace:
        if step["type"] == "arbitration":
            msg = (f"<b>ARBITRATION</b> on {DEVICE_LABEL.get(step['device'], step['device'])}: "
                   f"{step['winner']} overruled {', '.join(step['losers'])}")
            broadcast_log(conn, "arbitration", msg)
            socketio.emit("arbitration", step)
        else:
            rule_map = {r["id"]: r for r in rules}
            r = rule_map.get(step["rule"], {})
            cond = r.get("condition", {})
            msg = (f"<b>{step['rule']} TRIGGERED</b> — {cond.get('input')} {cond.get('operator')} "
                   f"{cond.get('value')} → {step['device']} = {step['value']}")
            broadcast_log(conn, "trigger", msg)
            socketio.emit("rule_triggered", step)

    socketio.emit("state_update", result.state)
    return result


# ---------------------------------------------------------------------------
# REST: state
# ---------------------------------------------------------------------------

@app.route("/api/state", methods=["GET"])
def api_get_state():
    conn = get_db()
    return jsonify(db.get_state(conn))


@app.route("/api/state", methods=["PATCH"])
def api_patch_state():
    """Manually set one or more sensor values (used by dashboard sliders
    in manual mode). Does NOT run the engine — call /api/engine/run after."""
    conn = get_db()
    patch = request.get_json(force=True) or {}
    db.set_state(conn, patch)
    socketio.emit("state_update", db.get_state(conn))
    for k, v in patch.items():
        broadcast_log(conn, "sensor", f"Sensor <b>{DEVICE_LABEL.get(k, k)}</b> manually set to {v}")
    return jsonify(db.get_state(conn))


# ---------------------------------------------------------------------------
# REST: rules
# ---------------------------------------------------------------------------

@app.route("/api/rules", methods=["GET"])
def api_get_rules():
    conn = get_db()
    return jsonify(db.get_all_rules(conn))


@app.route("/api/rules", methods=["POST"])
def api_add_rule():
    """Add a new rule. Rejects with 409 if it would create a dependency
    cycle in the trigger->action graph (checked BEFORE insertion)."""
    conn = get_db()
    body = request.get_json(force=True)

    existing = db.get_all_rules(conn)
    next_num = len(existing) + 1
    while any(r["id"] == f"R{next_num}" for r in existing):
        next_num += 1

    new_rule = {
        "id": body.get("id") or f"R{next_num}",
        "name": body.get("name", "Untitled Rule"),
        "condition": body["condition"],
        "action": body["action"],
        "priority": int(body.get("priority", 5)),
        "enabled": True,
    }

    cycle = engine.check_cycle_for(existing, new_rule)
    if cycle:
        db.bump_counters(conn, cyclesBlocked=1)
        story = " → ".join(cycle["story"])
        broadcast_log(conn, "cycle", f"<b>CYCLE DETECTED</b> — rejected {new_rule['id']}: {story}")
        socketio.emit("cycle_detected", {"rule": new_rule, "cycle_path": cycle["cycle_path"], "story": cycle["story"]})
        return jsonify({"ok": False, "error": "cycle_detected",
                         "cycle_path": cycle["cycle_path"], "story": story}), 409

    db.insert_rule(conn, new_rule)
    broadcast_log(conn, "system", f"<b>RULE ADDED</b> — {new_rule['id']} \"{new_rule['name']}\" activated live, no restart required")
    socketio.emit("rules_changed", db.get_all_rules(conn))
    execute_engine_and_broadcast(conn, reason="rule_added")
    return jsonify({"ok": True, "rule": new_rule}), 201


@app.route("/api/rules/<rule_id>", methods=["PATCH"])
def api_toggle_rule(rule_id):
    """Enable/disable a rule. Body: {"enabled": true|false}"""
    conn = get_db()
    body = request.get_json(force=True) or {}
    rules = db.get_all_rules(conn)
    rule = next((r for r in rules if r["id"] == rule_id), None)
    if not rule:
        return jsonify({"ok": False, "error": "not_found"}), 404

    enabled = bool(body.get("enabled", not rule["enabled"]))

    if enabled:
        # re-check cycle in case the graph changed since this rule was disabled
        others = [r for r in rules if r["id"] != rule_id]
        cycle = engine.check_cycle_for(others, {**rule, "enabled": True})
        if cycle:
            story = " → ".join(cycle["story"])
            broadcast_log(conn, "cycle", f"<b>CYCLE DETECTED</b> — cannot enable {rule_id}: {story}")
            return jsonify({"ok": False, "error": "cycle_detected", "story": story}), 409

    db.set_rule_enabled(conn, rule_id, enabled)
    broadcast_log(conn, "system", f"Rule <b>{rule_id}</b> {'enabled' if enabled else 'disabled'}")
    socketio.emit("rules_changed", db.get_all_rules(conn))
    execute_engine_and_broadcast(conn, reason="rule_toggled")
    return jsonify({"ok": True})


@app.route("/api/rules/<rule_id>", methods=["DELETE"])
def api_delete_rule(rule_id):
    conn = get_db()
    db.delete_rule(conn, rule_id)
    broadcast_log(conn, "system", f"Rule <b>{rule_id}</b> deleted")
    socketio.emit("rules_changed", db.get_all_rules(conn))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# REST: engine / graph / stats / log
# ---------------------------------------------------------------------------

@app.route("/api/engine/run", methods=["POST"])
def api_run_engine():
    conn = get_db()
    result = execute_engine_and_broadcast(conn, reason="manual_run")
    return jsonify({
        "state": result.state,
        "trace": result.trace,
        "executions": result.executions,
        "conflicts": result.conflicts,
    })


@app.route("/api/graph", methods=["GET"])
def api_get_graph():
    """Returns the device/rule dependency graph (nodes + edges) for the
    frontend to render, plus device metadata for labeling."""
    conn = get_db()
    rules = db.get_all_rules(conn)
    adj = engine.build_graph(rules)
    edges = [{"from": src, "to": dst} for src, targets in adj.items() for dst in targets]
    nodes = [{"id": k, "label": DEVICE_LABEL.get(k, k),
              "type": "sensor" if k in engine.SENSOR_KEYS else "actuator"} for k in ALL_DEVICE_KEYS]
    nodes += [{"id": f"RULE:{r['id']}", "label": r["id"], "type": "rule", "enabled": r["enabled"]} for r in rules]
    return jsonify({"nodes": nodes, "edges": edges})


@app.route("/api/stats", methods=["GET"])
def api_get_stats():
    conn = get_db()
    rules = db.get_all_rules(conn)
    counters = db.get_counters(conn)
    return jsonify({
        "active_rules": sum(1 for r in rules if r["enabled"]),
        "disabled_rules": sum(1 for r in rules if not r["enabled"]),
        "executions": counters.get("executions", 0),
        "conflicts": counters.get("conflicts", 0),
        "cycles_blocked": counters.get("cyclesBlocked", 0),
    })


@app.route("/api/log", methods=["GET"])
def api_get_log():
    conn = get_db()
    type_filter = request.args.get("type", "all")
    limit = int(request.args.get("limit", 80))
    return jsonify(db.get_log(conn, type_filter, limit))


# ---------------------------------------------------------------------------
# REST: canned demo scenarios (mirrors the prototype's judge-demo buttons)
# ---------------------------------------------------------------------------

@app.route("/api/scenario/<int:num>", methods=["POST"])
def api_scenario(num):
    conn = get_db()
    if num == 1:
        broadcast_log(conn, "system", "Demo Scenario 01 — Solar Surplus")
        db.set_state(conn, {"Solar_Output": 8})
        socketio.emit("state_update", db.get_state(conn))
        execute_engine_and_broadcast(conn)
        return jsonify({"ok": True, "scenario": "solar_surplus"})

    if num == 2:
        broadcast_log(conn, "system", "Demo Scenario 02 — Peak Tariff Conflict")
        db.set_state(conn, {"Building_Temp": 35, "Grid_Tariff_Price": 0.45})
        socketio.emit("state_update", db.get_state(conn))
        execute_engine_and_broadcast(conn)
        return jsonify({"ok": True, "scenario": "peak_tariff_conflict"})

    if num == 3:
        broadcast_log(conn, "system", "Demo Scenario 03 — Cycle Attack")
        existing = db.get_all_rules(conn)
        next_num = len(existing) + 1
        while any(r["id"] == f"R{next_num}" for r in existing):
            next_num += 1
        # Payload closes the loop with R1 (Solar_Output->EV_Charger) and R2
        # (EV_Charger->Battery_Discharge): Battery_Discharge -> attack ->
        # Solar_Output -> R1 -> EV_Charger -> R2 -> Battery_Discharge.
        attempt = {"id": f"R{next_num}", "enabled": True, **engine.CYCLE_ATTACK_PAYLOAD}
        cycle = engine.check_cycle_for(existing, attempt)
        if cycle:
            db.bump_counters(conn, cyclesBlocked=1)
            story = " → ".join(cycle["story"])
            broadcast_log(conn, "cycle", f"<b>CYCLE DETECTED</b> — rejected {attempt['id']}: {story}")
            socketio.emit("cycle_detected", {"rule": attempt, "cycle_path": cycle["cycle_path"], "story": cycle["story"]})
            return jsonify({"ok": True, "scenario": "cycle_attack", "rejected": True, "story": story})
        db.insert_rule(conn, attempt)
        socketio.emit("rules_changed", db.get_all_rules(conn))
        return jsonify({"ok": True, "scenario": "cycle_attack", "rejected": False})

    if num == 4:
        broadcast_log(conn, "system", "Demo Scenario 04 — Live Rule Injection")
        existing = db.get_all_rules(conn)
        next_num = len(existing) + 1
        while any(r["id"] == f"R{next_num}" for r in existing):
            next_num += 1
        new_rule = {
            "id": f"R{next_num}", "name": "Battery Cutoff (injected live)",
            "condition": {"input": "Battery_Level", "operator": "<", "value": 20},
            "action": {"device": "Battery_Discharge", "value": "OFF"},
            "priority": 9, "enabled": True,
        }
        cycle = engine.check_cycle_for(existing, new_rule)
        if cycle:
            return jsonify({"ok": False, "error": "cycle_detected"}), 409
        db.insert_rule(conn, new_rule)
        broadcast_log(conn, "system", f"<b>RULE ADDED</b> — {new_rule['id']} injected live, no restart required")
        socketio.emit("rules_changed", db.get_all_rules(conn))
        execute_engine_and_broadcast(conn)
        return jsonify({"ok": True, "scenario": "live_injection", "rule": new_rule})

    return jsonify({"ok": False, "error": "unknown_scenario"}), 400


# ---------------------------------------------------------------------------
# Auto-simulation background loop (mirrors the prototype's setInterval)
# ---------------------------------------------------------------------------

def auto_sim_loop():
    while True:
        time.sleep(3)
        with sim_lock:
            if sim_state["mode"] != "auto":
                continue
            sim_state["tick"] += 1
            t = sim_state["tick"] % 60

        with app.app_context():
            conn = db.get_connection()
            solar_curve = max(0, math.sin((t / 60) * math.pi) * 9.5)
            state = db.get_state(conn)
            patch = {
                "Solar_Output": round(solar_curve * 10) / 10,
                "Battery_Level": max(2, min(100, state.get("Battery_Level", 50) +
                                             (-0.6 if state.get("Battery_Discharge") == "ON"
                                              else (0.8 if solar_curve > 4 else 0.1)))),
                "Building_Temp": round((22 + math.sin((t / 60) * math.pi) * 8 + (random.random() - 0.5)) * 10) / 10,
            }
            if sim_state["tick"] % 12 == 0:
                patch["Grid_Tariff_Price"] = random.choice([0.12, 0.25, 0.45])
            db.set_state(conn, patch)
            socketio.emit("state_update", db.get_state(conn))
            execute_engine_and_broadcast(conn)
            conn.close()


@app.route("/api/sim/mode", methods=["POST"])
def api_sim_mode():
    """Body: {"mode": "auto"|"manual"}"""
    body = request.get_json(force=True) or {}
    mode = body.get("mode", "auto")
    with sim_lock:
        sim_state["mode"] = mode
    conn = get_db()
    broadcast_log(conn, "system", f"Switched to <b>{'AUTO SIMULATION' if mode == 'auto' else 'MANUAL DEMO'}</b> mode")
    return jsonify({"ok": True, "mode": mode})


@app.route("/api/sim/mode", methods=["GET"])
def api_sim_mode_get():
    with sim_lock:
        return jsonify({"mode": sim_state["mode"]})


# ---------------------------------------------------------------------------
# WebSocket lifecycle
# ---------------------------------------------------------------------------

@socketio.on("connect")
def on_connect():
    conn = db.get_connection()
    socketio.emit("state_update", db.get_state(conn), to=request.sid)
    socketio.emit("rules_changed", db.get_all_rules(conn), to=request.sid)
    conn.close()


if __name__ == "__main__":
    db.init_db()
    threading.Thread(target=auto_sim_loop, daemon=True).start()
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, allow_unsafe_werkzeug=True)
