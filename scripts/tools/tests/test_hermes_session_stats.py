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


# --- Timing fixture with display_kind / compacted columns -------------------
# 合成フィクスチャ。実在の state.db や実運用ログは使わない。
TIMING_SCHEMA_SQL = """
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
    display_kind TEXT,
    compacted INTEGER DEFAULT 0,
    timestamp REAL NOT NULL,
    token_count INTEGER
);
"""


def _build_timing_db() -> str:
    """Build a synthetic DB with a known timeline for classification tests.

    Timeline (seconds, wall clock = 1000s, session 'timed'):

      t=0   user
      t=10  assistant           -> 応答 [0,20)  model_processing (last output at 20)
      t=20  tool                -> 人間待ち [20,100) = 80s
      t=100 user
      t=110 assistant           -> 応答 [100,120) model_processing (last output at 120)
      t=120 tool clarify        -> 人間待ち [120,200) = 80s (承認待ち 80s)
      t=200 user
      t=210 assistant (failed_turn) -> 応答 [200,210) stream_retry
      t=300 user                -> 人間待ち [210,300) = 90s
      t=310 assistant (compacted)-> 応答 [300,320) compaction (last output at 320)
      t=320 tool
      t=400 user                -> 人間待ち [320,400) = 80s
      (tail)                    -> 未分類 [400,1000) = 600s

    Expected bucket sums (seconds):
      human_wait      = 80 + 80 + 90 + 80 = 330
      response total  = 20 + 20 + 10 + 20 = 70
        model         = 20 + 20 = 40
        stream_retry  = 10
        compaction    = 20
        approval      = 0
      breakdown total = 330 + 70 = 400
      unclassified    = 1000 - 400 = 600
    """
    f = tempfile.NamedTemporaryFile(prefix="test_timed_", suffix=".db", delete=False)
    f.close()
    conn = sqlite3.connect(f.name)
    conn.executescript(TIMING_SCHEMA_SQL)
    conn.execute(
        "INSERT INTO sessions (id, title, started_at, ended_at, api_call_count, "
        "message_count, output_tokens, tool_call_count) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("timed", "Timed session", 0.0, 1000.0, 8, 12, 100, 3),
    )
    rows = [
        # (role, tool_name, display_kind, compacted, ts, token_count)
        ("user", None, None, 0, 0.0, None),
        ("assistant", None, None, 0, 10.0, 10),
        ("tool", "read_file", None, 0, 20.0, None),
        ("user", None, None, 0, 100.0, None),
        ("assistant", None, None, 0, 110.0, 10),
        ("tool", "clarify", None, 0, 120.0, None),
        ("user", None, None, 0, 200.0, None),
        ("assistant", None, "failed_turn", 0, 210.0, 10),
        ("user", None, None, 0, 300.0, None),
        ("assistant", None, None, 1, 310.0, 10),
        ("tool", "write_file", None, 0, 320.0, None),
        ("user", None, None, 0, 400.0, None),
    ]
    conn.executemany(
        "INSERT INTO messages (session_id, role, tool_name, display_kind, compacted, "
        "timestamp, token_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [("timed", *r) for r in rows],
    )
    conn.commit()
    conn.close()
    return f.name


class TestTimeClassification(unittest.TestCase):
    """人間待ち/応答の向きと、内訳合計＋未分類＝壁時計の不変条件を検査する。"""

    def setUp(self) -> None:
        self.db_path = _build_timing_db()

    def tearDown(self) -> None:
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)

    def _breakdown(self) -> dict:
        r = _run_script(self.db_path, "--session", "timed", "--format", "json")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        return json.loads(r.stdout)["session"]["time_breakdown"]

    def test_human_wait_is_not_counted_as_response(self) -> None:
        """人間の入力待ちは人間待ちに入り、応答には入らない（向きの検査）。"""
        b = self._breakdown()
        # 人間待ち = 80+80+90+80 = 330s（承認待ち 80s を含む）。
        self.assertAlmostEqual(b["human_wait_secs"], 330.0, places=1)
        # 応答 = 20+20+10+20 = 70s。人間待ちの 330s が混入していないこと。
        self.assertAlmostEqual(b["response_secs"], 70.0, places=1)
        self.assertNotAlmostEqual(b["response_secs"], b["human_wait_secs"], places=1)

    def test_response_breakdown_buckets(self) -> None:
        """応答の内訳がログの根拠どおりに振り分けられる。"""
        b = self._breakdown()
        self.assertAlmostEqual(b["model_processing"], 40.0, places=1)
        self.assertAlmostEqual(b["stream_retry"], 10.0, places=1)
        self.assertAlmostEqual(b["compaction"], 20.0, places=1)

    def test_approval_wait_recorded(self) -> None:
        """clarify（承認要求）待ちが人間待ちの内数として記録される。"""
        b = self._breakdown()
        self.assertAlmostEqual(b["approval_wait_secs"], 80.0, places=1)

    def test_breakdown_plus_unclassified_equals_wall_clock(self) -> None:
        """内訳合計＋未分類＝壁時計 になる。"""
        b = self._breakdown()
        self.assertAlmostEqual(b["wall_clock_secs"], 1000.0, places=1)
        self.assertAlmostEqual(b["breakdown_total_secs"], 400.0, places=1)
        self.assertAlmostEqual(b["unclassified"], 600.0, places=1)
        self.assertAlmostEqual(
            b["breakdown_total_secs"] + b["unclassified"],
            b["wall_clock_secs"],
            places=1,
        )
        self.assertTrue(b["reconciles_with_wall_clock"])

    def test_response_secs_carries_deprecated_alias(self) -> None:
        """turn 出力に response_secs があり、旧名 human_wait_secs が同値で残る。"""
        r = _run_script(self.db_path, "--session", "timed", "--format", "json")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        turns = json.loads(r.stdout)["session"]["turn_analysis"]
        self.assertTrue(turns)
        for t in turns:
            self.assertIn("response_secs", t)
            self.assertIn("human_wait_secs", t)
            # 旧名は「応答」の意味で、応答値と同値（人間待ちではない）。
            self.assertEqual(t["human_wait_secs"], t["response_secs"])

    def test_text_output_labels_terms(self) -> None:
        """text 出力に固定した用語（人間待ち・応答・壁時計・未分類）が並ぶ。"""
        r = _run_script(self.db_path, "--session", "timed")
        self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
        for label in ("Human wait (人間待ち)", "Response (応答)",
                      "Wall clock (壁時計)", "Unclassified", "未分類"):
            self.assertIn(label, r.stdout)

    def test_missing_evidence_falls_to_unclassified(self) -> None:
        """根拠が無い区間は推測で分類せず未分類に入る（応答 0＋不変条件で検査）。"""
        f = tempfile.NamedTemporaryFile(prefix="test_noev_", suffix=".db", delete=False)
        f.close()
        try:
            conn = sqlite3.connect(f.name)
            conn.executescript(TIMING_SCHEMA_SQL)
            conn.execute(
                "INSERT INTO sessions (id, title, started_at, ended_at, api_call_count, "
                "message_count, output_tokens, tool_call_count) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("noev", "No evidence", 0.0, 500.0, 2, 2, 10, 0),
            )
            # assistant/tool も display_kind も compacted も無い＝分類の根拠なし。
            # 時間はすべて user↔user の隙間か、メッセージの外側の空白。
            conn.executemany(
                "INSERT INTO messages (session_id, role, tool_name, display_kind, "
                "compacted, timestamp, token_count) VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("noev", "user", None, None, 0, 100.0, None),
                    ("noev", "user", None, None, 0, 200.0, None),
                ],
            )
            conn.commit()
            conn.close()
            r = _run_script(f.name, "--session", "noev", "--format", "json")
            self.assertEqual(r.returncode, 0, f"stderr: {r.stderr}")
            b = json.loads(r.stdout)["session"]["time_breakdown"]
            # assistant 出力が一切無いので応答は 0（推測で埋めない）。
            self.assertAlmostEqual(b["response_secs"], 0.0, places=1)
            self.assertEqual(b["model_processing"], 0.0)
            self.assertEqual(b["stream_retry"], 0.0)
            self.assertEqual(b["compaction"], 0.0)
            # 内訳合計＋未分類＝壁時計 を満たすこと。
            self.assertAlmostEqual(
                b["breakdown_total_secs"] + b["unclassified"],
                b["wall_clock_secs"],
                places=1,
            )
            self.assertTrue(b["reconciles_with_wall_clock"])
        finally:
            if os.path.exists(f.name):
                os.unlink(f.name)


if __name__ == "__main__":
    unittest.main()