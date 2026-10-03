"""Test hermes_session_stats.py with an in-memory SQLite mock of Hermes state.db."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(SCRIPT_DIR, "hermes_session_stats.py")

MINIMAL_SCHEMA_SQL = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    title TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    api_call_count INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL,
    tool_name TEXT,
    timestamp REAL NOT NULL,
    token_count INTEGER
);
"""


def _build_minimal_db() -> str:
    """Create a temp SQLite DB with minimal schema and sample data. Return path."""
    f = tempfile.NamedTemporaryFile(prefix="test_state_", suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript(MINIMAL_SCHEMA_SQL)

    # Insert sessions
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, ended_at, api_call_count, "
        "message_count, output_tokens, tool_call_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("s1", "Translation session A", 1000.0, 2000.0, 10, 30, 5000, 15),
    )
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, ended_at, api_call_count, "
        "message_count, output_tokens, tool_call_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("s2", "Review session B", 3000.0, None, 5, 12, 2000, 8),
    )
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, ended_at, api_call_count, "
        "message_count, output_tokens, tool_call_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("s3", "QA session C", 4000.0, 5000.0, 20, 50, 15000, 30),
    )

    # Insert messages for s1 (includes skill_view calls)
    conn.executescript("""
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'user', NULL, 1100.0, NULL);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'assistant', NULL, 1150.0, 400);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'tool', 'read_file', 1160.0, NULL);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'tool', 'skill_view', 1170.0, NULL);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'tool', 'skill_view', 1180.0, NULL);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'assistant', NULL, 1200.0, 300);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'user', NULL, 1300.0, NULL);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'assistant', NULL, 1350.0, 500);
        INSERT INTO messages (session_id, role, tool_name, timestamp, token_count)
        VALUES ('s1', 'tool', 'write_file', 1360.0, NULL);
    """)

    conn.commit()
    conn.close()
    return f.name


def _run_script(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, SCRIPT_PATH, *args],
        capture_output=True, text=True, timeout=30,
    )


class TestHermesSessionStats(unittest.TestCase):

    def setUp(self) -> None:
        self.db_path = _build_minimal_db()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def test_list_default_last_5(self) -> None:
        """--last 5 outputs three sessions in text mode."""
        r = _run_script(self.db_path)
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        self.assertIn("Translation session A", r.stdout)
        self.assertIn("Review session B", r.stdout)
        self.assertIn("QA session C", r.stdout)
        self.assertIn("Session ID", r.stdout)

    def test_list_json_format(self) -> None:
        """--format json outputs JSON with sessions list."""
        r = _run_script(self.db_path, "--last", "5", "--format", "json")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        data = json.loads(r.stdout)
        self.assertIn("sessions", data)
        self.assertEqual(len(data["sessions"]), 3)

    def test_list_last_1(self) -> None:
        """--last 1 outputs only the most recent session (s3: QA session C)."""
        r = _run_script(self.db_path, "--last", "1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("QA session C", r.stdout)
        self.assertNotIn("Translation session A", r.stdout)
        self.assertNotIn("Review session B", r.stdout)

    def test_detail_by_session_id(self) -> None:
        """--session s1 shows detail with turn analysis."""
        r = _run_script(self.db_path, "--session", "s1")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        self.assertIn("Translation session A", r.stdout)
        self.assertIn("Turn-by-turn latency", r.stdout)
        self.assertIn("skill_view : 2", r.stdout)

    def test_detail_by_title(self) -> None:
        """--session matches by title."""
        r = _run_script(self.db_path, "--session", "Review session B")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        self.assertIn("Review session B", r.stdout)

    def test_detail_json(self) -> None:
        """--session with --format json."""
        r = _run_script(self.db_path, "--session", "s1", "--format", "json")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        data = json.loads(r.stdout)
        self.assertIn("session", data)
        self.assertIn("turn_analysis", data["session"])
        self.assertEqual(data["session"]["skill_view_count"], 2)

    def test_detail_not_found(self) -> None:
        """--session with unknown id returns exit 0 but prints error."""
        r = _run_script(self.db_path, "--session", "nonexistent")
        # Not found is not fatal exit, we print error to stderr
        self.assertIn("session not found", r.stderr)

    def test_db_not_found(self) -> None:
        """Missing DB → exit code 1."""
        r = _run_script("/tmp/nonexistent_state_123456.db")
        self.assertEqual(r.returncode, 1)
        self.assertIn("database not found", r.stderr)

    def test_unsupported_schema(self) -> None:
        """DB without sessions table → exit code 2."""
        f = tempfile.NamedTemporaryFile(prefix="test_bad_", suffix=".db", delete=False)
        f.close()
        try:
            conn = sqlite3.connect(f.name)
            conn.execute("CREATE TABLE unrelated (id INTEGER PRIMARY KEY)")
            conn.commit()
            conn.close()
            r = _run_script(f.name)
            self.assertEqual(r.returncode, 2)
            self.assertIn("unsupported schema", r.stderr)
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)

    def test_partial_schema_warning(self) -> None:
        """DB missing some columns but has sessions → exit 2 with output + warning."""
        f = tempfile.NamedTemporaryFile(prefix="test_partial_", suffix=".db", delete=False)
        f.close()
        try:
            conn = sqlite3.connect(f.name)
            conn.executescript("""
                CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, started_at REAL);
                CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL);
            """)
            conn.execute(
                "INSERT INTO sessions (id, title, started_at) VALUES (?, ?, ?)",
                ("s1", "Partial", 1000.0),
            )
            conn.commit()
            conn.close()
            r = _run_script(f.name, "--last", "5")
            self.assertEqual(r.returncode, 2)
            # Partial schema should still print what it can
            self.assertIn("Partial", r.stdout)
            self.assertIn("warning:", r.stderr)
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)

    def test_avg_output_tokens(self) -> None:
        """Verify avg_output_tokens_per_call is computed for s1 (5000/10=500.0)."""
        r = _run_script(self.db_path, "--session", "s1", "--format", "json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        self.assertEqual(data["session"]["avg_output_tokens_per_call"], 500.0)

    def test_running_session_duration(self) -> None:
        """Session s2 has no ended_at → duration shows (running)."""
        r = _run_script(self.db_path, "--last", "5", "--format", "json")
        self.assertEqual(r.returncode, 0)
        data = json.loads(r.stdout)
        for s in data["sessions"]:
            if s["session_id"] == "s2":
                self.assertEqual(s["duration"], "(running)")
                break
        else:
            self.fail("s2 not found")


if __name__ == "__main__":
    unittest.main()