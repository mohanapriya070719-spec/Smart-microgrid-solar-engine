"""
SQLite persistence for the Microgrid Energy Manager backend.

Uses plain sqlite3 (no ORM) to keep the hackathon footprint small.
One connection per request via Flask's `g` — see app.py's get_db().
"""

import json
import sqlite3
from pathlib import Path

from engine import DEFAULT_RULES, DEFAULT_STATE

DB_PATH = Path(__file__).parent / "microgrid.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    condition_input TEXT NOT NULL,
    condition_operator TEXT NOT NULL,
    condition_value TEXT NOT NULL,
    action_device TEXT NOT NULL,
    action_value TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 5,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS device_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    msg TEXT NOT NULL,
    time TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS counters (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(reset: bool = False):
    """Create tables if they don't exist and seed default rules/state
    on first run. Set reset=True to wipe and reseed (useful for demos)."""
    if reset and DB_PATH.exists():
        DB_PATH.unlink()

    conn = get_connection()
    conn.executescript(SCHEMA)

    already_seeded = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
    if not already_seeded:
        for r in DEFAULT_RULES:
            _insert_rule(conn, r)
        for k, v in DEFAULT_STATE.items():
            conn.execute(
                "INSERT OR REPLACE INTO device_state (key, value) VALUES (?, ?)",
                (k, json.dumps(v)),
            )
        for c in ("executions", "conflicts", "cyclesBlocked"):
            conn.execute("INSERT OR REPLACE INTO counters (key, value) VALUES (?, 0)", (c,))
        conn.execute("INSERT INTO meta (key, value) VALUES ('seeded', '1')")
        conn.commit()
    conn.close()


def _insert_rule(conn: sqlite3.Connection, r: dict):
    conn.execute(
        """INSERT OR REPLACE INTO rules
           (id, name, condition_input, condition_operator, condition_value,
            action_device, action_value, priority, enabled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            r["id"], r["name"],
            r["condition"]["input"], r["condition"]["operator"], json.dumps(r["condition"]["value"]),
            r["action"]["device"], json.dumps(r["action"]["value"]),
            r["priority"], 1 if r["enabled"] else 0,
        ),
    )


def row_to_rule(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "condition": {
            "input": row["condition_input"],
            "operator": row["condition_operator"],
            "value": json.loads(row["condition_value"]),
        },
        "action": {
            "device": row["action_device"],
            "value": json.loads(row["action_value"]),
        },
        "priority": row["priority"],
        "enabled": bool(row["enabled"]),
    }


def get_all_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM rules ORDER BY id").fetchall()
    return [row_to_rule(r) for r in rows]


def insert_rule(conn: sqlite3.Connection, rule: dict):
    _insert_rule(conn, rule)
    conn.commit()


def delete_rule(conn: sqlite3.Connection, rule_id: str):
    conn.execute("DELETE FROM rules WHERE id=?", (rule_id,))
    conn.commit()


def set_rule_enabled(conn: sqlite3.Connection, rule_id: str, enabled: bool):
    conn.execute("UPDATE rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))
    conn.commit()


def get_state(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM device_state").fetchall()
    return {r["key"]: json.loads(r["value"]) for r in rows}


def set_state(conn: sqlite3.Connection, patch: dict):
    for k, v in patch.items():
        conn.execute(
            "INSERT OR REPLACE INTO device_state (key, value) VALUES (?, ?)",
            (k, json.dumps(v)),
        )
    conn.commit()


def log_event(conn: sqlite3.Connection, type_: str, msg: str) -> dict:
    cur = conn.execute("INSERT INTO event_log (type, msg) VALUES (?, ?)", (type_, msg))
    conn.commit()
    row = conn.execute("SELECT * FROM event_log WHERE id=?", (cur.lastrowid,)).fetchone()
    return {"id": row["id"], "type": row["type"], "msg": row["msg"], "time": row["time"]}


def get_log(conn: sqlite3.Connection, type_filter: str | None = None, limit: int = 80) -> list[dict]:
    if type_filter and type_filter != "all":
        rows = conn.execute(
            "SELECT * FROM event_log WHERE type=? ORDER BY id DESC LIMIT ?",
            (type_filter, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM event_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [{"id": r["id"], "type": r["type"], "msg": r["msg"], "time": r["time"]} for r in rows]


def get_counters(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM counters").fetchall()
    return {r["key"]: r["value"] for r in rows}


def bump_counters(conn: sqlite3.Connection, **deltas: int):
    for k, delta in deltas.items():
        if delta:
            conn.execute(
                "UPDATE counters SET value = value + ? WHERE key = ?", (delta, k)
            )
    conn.commit()
