"""Short atomic state transitions; never hold a database lock during inference."""
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import time


def data_directory():
    # Stable across plugin cache upgrades and standalone CLI invocations.
    configured = os.environ.get("CODEX_ADVISOR_DATA")
    return Path(configured).expanduser() if configured else Path.home() / ".local/share/codex-advisor"


def fresh(session_id, cwd=""):
    return dict(id=session_id, cwd=cwd, generation=0, revision=0, turn_id="",
                status="watching", paused=False, interrupted=False, ended=False,
                job=None, events=[], findings=[], history=[], prompt="", model="",
                reviews=0, input_tokens=0, output_tokens=0, corrections=0,
                continuation="", last_review=0, error=None, updated=time.time())


def record(state, kind, message):
    state["history"] = (state["history"] + [dict(at=time.time(), kind=kind, message=message)])[-80:]


class Store:
    def __init__(self, directory=None):
        self.directory = Path(directory or data_directory())
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "advisor.sqlite3"
        with self.connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, body TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)")
        self.path.chmod(0o600)

    def _connect(self):
        conn = sqlite3.connect(str(self.path), timeout=0.6)
        conn.execute("PRAGMA busy_timeout=600")
        return conn

    @contextlib.contextmanager
    def connect(self):
        conn = self._connect()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def transaction(self, session_id, cwd=""):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
            state = json.loads(row[0]) if row else fresh(session_id, cwd)
            yield state
            state["updated"] = time.time()
            conn.execute("INSERT OR REPLACE INTO sessions VALUES (?, ?)", (session_id, json.dumps(state)))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get(self, session_id):
        with self.connect() as conn:
            row = conn.execute("SELECT body FROM sessions WHERE id=?", (session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self.connect() as conn:
            rows = conn.execute("SELECT body FROM sessions").fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda s: s["updated"], reverse=True)

    def settings(self):
        with self.connect() as conn:
            row = conn.execute("SELECT body FROM settings WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {}

    def configure(self, values):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT body FROM settings WHERE id=1").fetchone()
            config = json.loads(row[0]) if row else {}
            config.update(values)
            conn.execute("INSERT OR REPLACE INTO settings VALUES (1, ?)", (json.dumps(config),))
        return config

    def pause(self, session_id, paused):
        if self.get(session_id) is None:
            raise ValueError("Session not found")
        with self.transaction(session_id) as state:
            state.update(paused=paused, generation=state["generation"] + 1, job=None)
            state["status"] = "paused" if paused else "watching"
            record(state, "control", "Paused by you" if paused else "Resumed; the next hook can review new activity")
