# Smart-microgrid-solar-engine

A DAG-based IoT rule engine that manages a simulated microgrid — solar, battery,
EV charging, HVAC, and grid tariff — in real time. Built as a single self-contained
prototype: open the HTML file and everything runs client-side, no server or
install step required.

> Instead of blindly executing hundreds of IF‑ELSE statements, this system uses a
> dependency-aware rule engine that understands relationships between devices,
> resolves competing decisions, prevents dangerous feedback loops, and adapts to
> new rules without shutting down.

---

## Quick start

```bash
open microgrid-energy-manager.html
```

That's it — it's a static HTML file with embedded CSS/JS. Double-click it or drag
it into any modern browser. No build step, no dependencies, no server.

---

## What it demonstrates

| Feature | Where to see it |
|---|---|
| **Rule chaining** | Demo Scenario 01, or push Solar Output above 7 kW manually |
| **Conflict arbitration** | Demo Scenario 02, or set Temp > 30°C and Tariff > $0.30 together |
| **Cycle detection** | Demo Scenario 03 — attempts to add a rule that would create a feedback loop |
| **Live rule updates** | Demo Scenario 04, or the **+ Add Rule** panel — no reload needed |
| **Guided walkthrough** | **🚀 Judge Demo Mode** button — runs all four in sequence, ~13 seconds |

---

## Architecture

```
Sensors (simulated) ──▶ Rule Engine ──▶ Decision Layer ──▶ Actuators
                              │
                    Conflict Resolver
                              │
                    Event / Audit Log
                              │
                       Live Dashboard
```

Everything lives in one HTML file, organized into logical modules within the
`<script>` block:

- **Domain model** — sensor/actuator definitions, initial state, preloaded rules
- **Condition evaluator** — numeric/string-aware comparison for `> < >= <= == !=`
- **Graph builder + cycle detector** — builds a directed graph from rule
  dependencies and runs DFS back-edge detection before any rule is committed
- **Rule engine (`runEngine`)** — the core loop: evaluate → arbitrate → apply →
  repeat until stable (or a safety cap of 20 iterations), which is what makes
  chaining and arbitration both fall out of the same mechanism
- **Rule management** — add / enable / disable / delete, all live
- **Render layer** — sensors, actuators, stats, energy intelligence, rule list,
  SVG dependency graph, explainability trace, filtered audit log
- **Simulation loop** — auto mode advances a simple day-cycle every 3s; manual
  mode lets you set exact values and click **Run Engine**
- **Judge Demo Mode** — scripted sequence through all four core features

### Data model

Rules are plain objects:

```json
{
  "id": "R1",
  "name": "Solar EV Charging",
  "condition": { "input": "Solar_Output", "operator": ">", "value": 7 },
  "action": { "device": "EV_Charger", "value": "ON" },
  "priority": 2,
  "enabled": true
}
```

### How chaining + arbitration actually work

Each call to `runEngine()`:

1. Evaluates every **enabled** rule's condition against current state.
2. Groups the rules whose condition is currently true ("claims") by the
   actuator they target.
3. For each actuator with more than one claim, sorts by `priority` (descending)
   and applies the winner — losers are logged as overruled.
4. Applies all winning actions to state.
5. If nothing changed this pass, stops. If something changed, loops again —
   this re-evaluation is what lets one rule's output satisfy the next rule's
   condition (true chaining), capped at 20 iterations as a safety guard against
   runaway cascades.

### How cycle detection works

Before a new rule is accepted, the app builds a **test graph** = existing rules
+ the candidate rule, where nodes are sensors/actuators plus one node per rule,
and edges run `condition.input → RULE:id → action.device`. A DFS with
white/gray/black coloring detects back-edges (a node revisited while still
"in progress" on the current path); if found, the full cycle path is
reconstructed and the rule is rejected before it ever touches the active graph.

---

## Preloaded rules

| ID | Name | Condition | Action | Priority |
|---|---|---|---|---|
| R1 | Solar EV Charging | `Solar_Output > 7` | `EV_Charger = ON` | 2 |
| R2 | Preserve Battery | `EV_Charger == ON` | `Battery_Discharge = OFF` | 4 |
| R3 | High Temperature Cooling | `Building_Temp > 30` | `HVAC_System = MAX` | 3 |
| R4 | Peak Tariff Saving | `Grid_Tariff_Price > 0.30` | `HVAC_System = ECO` | 8 |
| R5 | Low Battery Protection | `Battery_Level < 20` | `Battery_Discharge = OFF` | 10 |
| R6 | Solar Feedback (demo) | `Battery_Discharge == ON` | `Solar_Output = 10` | 1 |

R6 exists specifically so the Cycle Attack scenario has a real dependency
(`Battery_Discharge → Solar_Output`) to conflict with when it attempts to add
`Solar_Output → Battery_Discharge`.

---

## Using it

- **Auto Sim mode** (default): sensors drift on their own every 3 seconds,
  simulating a rough day cycle (solar rises/falls, tariff shifts periodically,
  temperature wanders, battery drains/charges based on actuator state). The
  engine re-runs on every tick.
- **Manual mode**: drag sensor sliders to any value, then click **Run Engine**
  to trigger evaluation on demand — useful for hitting exact demo numbers.
- **+ Add Rule**: opens a rule builder (name, condition, action, priority).
  Submitting runs full validation, cycle detection, and — if accepted —
  immediate activation and re-evaluation.
- **Rule list**: pause/resume or delete any rule live; the engine and graph
  update instantly.
- **Event log**: filterable by execution, arbitration, cycle, sensor, or
  system events.
- **Explainability panel**: shows the exact chain of conditions and winners
  behind the most recent engine run.

---

## Known limitations (prototype scope)

This build prioritizes a working, demonstrable rule engine over the full
production stack described in the original spec:

- **No backend / persistence** — everything runs in browser memory; refreshing
  the page resets state, rules, and logs. There's no Node/Express API, no
  WebSocket server, and no SQLite/Postgres storage.
- **No REST API surface** — the spec's `/api/rules`, `/api/graph`, etc. don't
  exist as real endpoints; all "API" behavior is internal function calls.
- **Single-user, single-tab** — no multi-client sync.

If you need the full multi-service version (Express + Socket.IO + a real
database, matching the original architecture doc), that's a separate build —
this prototype is meant to prove out and demo the rule-engine logic itself.

---

## File

- `microgrid-energy-manager.html` — the entire application (HTML + CSS + JS,
  no external JS dependencies beyond a Google Fonts stylesheet link)
