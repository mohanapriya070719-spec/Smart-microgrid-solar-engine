# Microgrid Rule Engine — Backend

Flask + SQLite + WebSocket backend for the Microgrid Energy Manager prototype.
Moves the rule engine (chaining, priority arbitration, DFS cycle detection)
out of the browser and into a real service, so the frontend becomes a thin
client that renders whatever the server computes and broadcasts.

## Setup

```bash
pip install -r requirements.txt
python app.py
```

Runs on `http://localhost:5000`. First run creates `microgrid.db` (SQLite)
and seeds it with the prototype's default 5 rules and sensor state.

> **Sandbox note:** `engine.py` and `database.py` were fully tested in this
> environment (pure Python, no external deps) and behave correctly. `app.py`
> (Flask-SocketIO layer) was syntax-checked but not run end-to-end here — no
> network access to `pip install flask-socketio`/`flask-cors`. Install and
> run it locally before your demo.

## A bug I found and fixed while porting

The original prototype's default rule set included a 6th rule, **"Solar
Feedback (demo)"** (`Battery_Discharge==ON → Solar_Output=10`), permanently
enabled. Combined with R1 (`Solar_Output→EV_Charger`) and R2
(`EV_Charger→Battery_Discharge`), it closes a structural loop back to
`Solar_Output`. Since cycle detection scans the whole graph of currently
active rules, that meant **every** future rule addition — including safe
ones like the Scenario 4 "Battery Cutoff" live-injection demo — would get
rejected as a false-positive cycle. I verified this by running the original
JS's exact `buildGraph`/`detectCycle` logic in Node directly.

Fix: dropped that rule from the standing default set and instead use it as
the **payload for the Scenario 3 "cycle attack" demo** (`engine.
CYCLE_ATTACK_PAYLOAD`) — it still gets rejected live when attempted, which
was the original intent, but no longer poisons the graph permanently. Cycle
detection was also changed to only consider currently-*enabled* rules,
since a disabled rule can't actually fire and shouldn't block unrelated new
ones.

## Architecture

- **`engine.py`** — pure logic, no Flask/DB: condition evaluation, graph
  building, DFS cycle detection (white/gray/black), and the chaining +
  priority-arbitration fixed-point loop. Fully unit-testable in isolation.
- **`database.py`** — SQLite persistence (plain `sqlite3`, no ORM): rules,
  device state, event log, counters.
- **`app.py`** — Flask REST endpoints + Flask-SocketIO WebSocket broadcasts
  + the auto-simulation background thread (mirrors the prototype's
  `setInterval` day-cycle simulation).

## REST API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/state` | current sensor/actuator values |
| PATCH | `/api/state` | manually set sensor value(s) — `{"Solar_Output": 8}` |
| GET | `/api/rules` | list all rules |
| POST | `/api/rules` | add a rule — `409` if it creates a cycle |
| PATCH | `/api/rules/<id>` | `{"enabled": true\|false}` |
| DELETE | `/api/rules/<id>` | delete a rule |
| POST | `/api/engine/run` | run engine once against current state, returns trace |
| GET | `/api/graph` | full node/edge graph for visualization |
| GET | `/api/stats` | active/disabled rule counts, executions, conflicts, cycles blocked |
| GET | `/api/log?type=&limit=` | event log, optionally filtered |
| POST | `/api/scenario/<1-4>` | canned judge-demo scenarios |
| POST | `/api/sim/mode` | `{"mode": "auto"\|"manual"}` |

## WebSocket events (server → client)

- `state_update` — full sensor/actuator state after any change
- `rule_triggered` — `{rule, device, value, iter}` per trace step
- `arbitration` — `{device, winner, losers, iter}` per conflict resolved
- `cycle_detected` — `{rule, cycle_path, story}` when a rule is rejected
- `rules_changed` — full rule list after add/toggle/delete
- `log_event` — `{type, msg, time}` for the event log panel

Connect with `socket.io-client` pointed at `http://localhost:5000`; on
connect the server immediately emits `state_update` and `rules_changed` so
the dashboard can render without waiting for the next tick.

## Wiring the existing frontend

The uploaded HTML currently keeps `rules`/`state` in JS variables and calls
`runEngine()` locally. To point it at this backend:

1. Replace the initial `let rules = [...]` / `let state = {...}` with
   fetches from `GET /api/rules` and `GET /api/state` on load.
2. Replace `tryAddRule()` / `toggleRule()` / `deleteRule()` bodies with
   `POST/PATCH/DELETE /api/rules...` calls; handle the `409` cycle response
   the same way the UI already handles `tryAddRule`'s `{ok:false}` case.
3. Replace the `runEngine()` call with `POST /api/engine/run` (or just let
   WebSocket `state_update`/`rule_triggered`/`arbitration` events drive
   `renderAll()` directly — no need to call it manually at all once
   connected).
4. Replace `setInterval(...)` auto-sim with `POST /api/sim/mode` to toggle
   server-side simulation; the server now owns the tick loop and pushes
   `state_update` every 3s itself.
5. Add `<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>`
   and connect: `const socket = io('http://localhost:5000');` then wire the
   events above into the existing `renderSensors/renderActuators/
   renderTrace/renderGraph/logEvent` functions.

Happy to wire this into the actual HTML file directly if you'd rather I
edit it in place instead of leaving it as an integration guide.
