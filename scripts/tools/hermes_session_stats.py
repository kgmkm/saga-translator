"""saga-translator: Hermes state.db セッション集計スクリプト.

Usage:
    python3 scripts/tools/hermes_session_stats.py <state.db パス> [--session <id-or-title>] [--last N] [--format json|text]

集計項目:
    - 壁時計 / API コール数 / 1コールの平均出力トークン数
    - skill_view の呼び出し回数
    - 人間待ち時間（推定: user メッセージから次の assistant メッセージまでの gap）
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

EXPECTED_TABLES = {"sessions", "messages"}
SESSION_COLS = {
    "id", "title", "started_at", "ended_at", "api_call_count",
    "message_count", "output_tokens", "tool_call_count",
}
MESSAGE_COLS = {"id", "session_id", "role", "tool_name", "timestamp", "token_count"}


def _session_cols_present(cursor) -> set[str]:
    """Return the subset of SESSION_COLS actually present in the sessions table."""
    cursor.execute("PRAGMA table_info(sessions)")
    present = {row[1] for row in cursor.fetchall()}
    return SESSION_COLS & present


def _message_cols_present(cursor) -> set[str]:
    """Return the subset of MESSAGE_COLS actually present in the messages table."""
    cursor.execute("PRAGMA table_info(messages)")
    present = {row[1] for row in cursor.fetchall()}
    return MESSAGE_COLS & present


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "(running)"
    try:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except (OSError, ValueError, OverflowError):
        return f"{ts:.3f}"


def _fmt_duration(secs: float | None) -> str:
    if secs is None:
        return "(running)"
    h, r = divmod(int(secs), 3600)
    m, s = divmod(r, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _check_schema(cursor) -> list[str]:
    """Return warning list; present if unexpected table schema is found."""
    warnings: list[str] = []
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    actual_tables = {row[0] for row in cursor.fetchall()}

    missing = EXPECTED_TABLES - actual_tables
    if missing:
        warnings.append(f"required table(s) not found: {', '.join(sorted(missing))}")
        # We still try to continue with whatever is available.

    # Verify columns for sessions table
    if "sessions" in actual_tables:
        cursor.execute("PRAGMA table_info(sessions)")
        cols = {row[1] for row in cursor.fetchall()}
        missing_cols = SESSION_COLS - cols
        if missing_cols:
            warnings.append(f"sessions table missing columns: {', '.join(sorted(missing_cols))}")

    if "messages" in actual_tables:
        cursor.execute("PRAGMA table_info(messages)")
        cols = {row[1] for row in cursor.fetchall()}
        missing_cols = MESSAGE_COLS - cols
        if missing_cols:
            warnings.append(f"messages table missing columns: {', '.join(sorted(missing_cols))}")

    return warnings


def _list_sessions(cursor, last_n: int, fmt: str) -> tuple[list[str], list[dict]]:
    """Return (warnings, rows)."""
    warnings: list[str] = []

    s_cols = _session_cols_present(cursor)
    if not s_cols:
        warnings.append("sessions table has no usable columns")
        if fmt == "json":
            _print_json({"sessions": [], "warnings": warnings})
        return warnings, []

    # Build column list using only present columns
    col_list = []
    for c in ("id", "title", "started_at", "ended_at", "api_call_count",
              "message_count", "output_tokens", "tool_call_count"):
        if c in s_cols:
            col_list.append(c)
    select_clause = ", ".join(col_list)

    query = f"SELECT {select_clause} FROM sessions ORDER BY started_at DESC LIMIT ?"
    cursor.execute(query, (last_n,))
    raw = cursor.fetchall()

    rows: list[dict] = []
    for row in raw:
        row_map = dict(zip(col_list, row))
        d: dict = {
            "session_id": row_map.get("id", ""),
            "title": row_map.get("title") or "",
        }
        started_at = row_map.get("started_at")
        ended_at = row_map.get("ended_at")
        d["started_at"] = _fmt_ts(started_at) if started_at is not None else ""
        d["ended_at"] = _fmt_ts(ended_at) if ended_at is not None else ""
        d["duration"] = (
            _fmt_duration(ended_at - started_at)
            if ended_at is not None and started_at is not None
            else "(running)"
        )

        d["api_call_count"] = row_map.get("api_call_count", 0) or 0
        d["message_count"] = row_map.get("message_count", 0) or 0
        d["output_tokens"] = row_map.get("output_tokens", 0) or 0
        d["tool_call_count"] = row_map.get("tool_call_count", 0) or 0

        # Skill_view count from messages
        d["skill_view_count"] = 0
        if "messages" in [
            r[0] for r in cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
            ).fetchall()
        ]:
            m_cols = _message_cols_present(cursor)
            if "session_id" in m_cols and "tool_name" in m_cols:
                try:
                    cursor.execute(
                        "SELECT COUNT(*) FROM messages WHERE session_id=? AND tool_name=?",
                        (d["session_id"], "skill_view"),
                    )
                    d["skill_view_count"] = cursor.fetchone()[0] or 0
                except sqlite3.OperationalError:
                    pass

        # Average output tokens per API call
        ac = d["api_call_count"]
        ot = d["output_tokens"]
        d["avg_output_tokens_per_call"] = round(ot / ac, 1) if ac > 0 else 0.0

        rows.append(d)

    if fmt == "text":
        _print_text_list(rows, warnings)
    else:
        _print_json({"sessions": rows, "warnings": warnings if warnings else None})

    return warnings, rows


def _print_text_list(rows: list[dict], warnings: list[str]) -> None:
    print("=== Recent sessions ===")
    print("")
    for r in rows:
        print(f"Session ID : {r['session_id']}")
        print(f"Title      : {r['title']}")
        print(f"Started    : {r['started_at']}")
        print(f"Ended      : {r['ended_at']}")
        print(f"Duration   : {r['duration']}")
        print(f"API calls  : {r['api_call_count']}")
        print(f"Messages   : {r['message_count']}")
        print(f"Output tkns: {r['output_tokens']}")
        print(f"Avg tkn/call: {r['avg_output_tokens_per_call']}")
        print(f"Tool calls : {r['tool_call_count']}")
        print(f"skill_view : {r['skill_view_count']}")
        print("")

    if warnings:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)


def _print_json(data: dict) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


def _detail_session(cursor, session_id_or_title: str, fmt: str) -> tuple[list[str], dict | None]:
    warnings: list[str] = []

    s_cols = _session_cols_present(cursor)
    if not s_cols:
        msg = "sessions table has no usable columns"
        if fmt == "json":
            _print_json({"error": msg})
        else:
            print(msg, file=sys.stderr)
        return warnings, None

    # Build column list
    col_list = []
    for c in ("id", "title", "started_at", "ended_at", "api_call_count",
              "message_count", "output_tokens", "tool_call_count"):
        if c in s_cols:
            col_list.append(c)
    select_clause = ", ".join(col_list)

    cursor.execute(
        f"SELECT {select_clause} FROM sessions WHERE id=? OR title=? "
        "ORDER BY started_at DESC LIMIT 1",
        (session_id_or_title, session_id_or_title),
    )
    row = cursor.fetchone()
    if not row:
        msg = f"session not found: {session_id_or_title}"
        if fmt == "json":
            _print_json({"error": msg})
        else:
            print(msg, file=sys.stderr)
        return warnings, None

    row_map = dict(zip(col_list, row))
    sid = row_map.get("id", "")
    started_at = row_map.get("started_at")
    ended_at = row_map.get("ended_at")

    base = {
        "session_id": sid,
        "title": row_map.get("title") or "",
        "started_at": _fmt_ts(started_at) if started_at is not None else "",
        "ended_at": _fmt_ts(ended_at) if ended_at is not None else "",
        "duration": (
            _fmt_duration(ended_at - started_at)
            if ended_at is not None and started_at is not None
            else "(running)"
        ),
        "api_call_count": row_map.get("api_call_count", 0) or 0,
        "message_count": row_map.get("message_count", 0) or 0,
        "output_tokens": row_map.get("output_tokens", 0) or 0,
        "tool_call_count": row_map.get("tool_call_count", 0) or 0,
    }

    # skill_view count
    base["skill_view_count"] = 0
    if "messages" in [
        r[0] for r in cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='messages'"
        ).fetchall()
    ]:
        m_cols = _message_cols_present(cursor)
        if "session_id" in m_cols and "tool_name" in m_cols:
            try:
                cursor.execute(
                    "SELECT COUNT(*) FROM messages WHERE session_id=? AND tool_name=?",
                    (sid, "skill_view"),
                )
                base["skill_view_count"] = cursor.fetchone()[0] or 0
            except sqlite3.OperationalError:
                pass

    ac = base["api_call_count"]
    ot = base["output_tokens"]
    base["avg_output_tokens_per_call"] = round(ot / ac, 1) if ac > 0 else 0.0

    # Turn-by-turn analysis from messages
    turn_analysis = _compute_turn_analysis(cursor, sid)
    base["turn_analysis"] = turn_analysis

    if fmt == "json":
        _print_json({"session": base, "warnings": warnings if warnings else None})
    else:
        _print_text_detail(base, turn_analysis, warnings)

    return warnings, base


def _compute_turn_analysis(cursor, session_id: str) -> list[dict]:
    """Compute turn-level latency from messages table."""
    m_cols = _message_cols_present(cursor)
    if "session_id" not in m_cols or "role" not in m_cols or "timestamp" not in m_cols:
        return []

    has_token_count = "token_count" in m_cols
    has_tool_name = "tool_name" in m_cols

    try:
        cursor.execute(
            "SELECT role, timestamp, token_count, tool_name "
            "FROM messages WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        )
        msgs = cursor.fetchall()
    except sqlite3.OperationalError:
        return []

    turns: list[dict] = []
    i = 0
    while i < len(msgs):
        role, ts, tkn, tname = msgs[i]
        if role == "user":
            # Find next assistant+tools block
            assistant_idx = None
            for j in range(i + 1, len(msgs)):
                if msgs[j][0] == "assistant":
                    assistant_idx = j
                    break
            if assistant_idx is not None:
                latency = msgs[assistant_idx][1] - ts if msgs[assistant_idx][1] > ts else 0.0
                # Count tool calls in this block (after assistant, before next user)
                tool_calls = 0
                block_out_tokens = 0
                for j in range(assistant_idx, len(msgs)):
                    if msgs[j][0] == "user":
                        break
                    if msgs[j][3] is not None:  # tool_name present
                        tool_calls += 1
                    if msgs[j][0] == "assistant" and msgs[j][2] is not None:
                        block_out_tokens += msgs[j][2]

                turns.append({
                    "turn_index": len(turns),
                    "user_at": _fmt_ts(ts),
                    "response_start": _fmt_ts(msgs[assistant_idx][1]),
                    "human_wait_secs": round(latency, 1),
                    "tool_calls_in_block": tool_calls,
                    "output_tokens": block_out_tokens,
                })
            i = assistant_idx + 1 if assistant_idx is not None else i + 1
        else:
            i += 1

    return turns


def _print_text_detail(base: dict, turn_analysis: list[dict], warnings: list[str]) -> None:
    print("=== Session detail ===")
    print("")
    print(f"Session ID : {base['session_id']}")
    print(f"Title      : {base['title']}")
    print(f"Started    : {base['started_at']}")
    print(f"Ended      : {base['ended_at']}")
    print(f"Duration   : {base['duration']}")
    print(f"API calls  : {base['api_call_count']}")
    print(f"Messages   : {base['message_count']}")
    print(f"Output tkns: {base['output_tokens']}")
    print(f"Avg tkn/call: {base['avg_output_tokens_per_call']}")
    print(f"Tool calls : {base['tool_call_count']}")
    print(f"skill_view : {base['skill_view_count']}")
    print("")

    if turn_analysis:
        print("--- Turn-by-turn latency ---")
        for t in turn_analysis:
            print(
                f"  Turn {t['turn_index']}: user@{t['user_at']} "
                f"→ response in {t['human_wait_secs']}s "
                f"(tools={t['tool_calls_in_block']}, out_tkns={t['output_tokens']})"
            )
        print("")
        # Summary
        waits = [t["human_wait_secs"] for t in turn_analysis]
        total_human_wait = sum(waits)
        avg_human_wait = round(total_human_wait / len(waits), 1) if waits else 0.0
        print(f"  Total human wait time: {_fmt_duration(total_human_wait)}")
        print(f"  Avg human wait per turn: {avg_human_wait}s")
        print("")

    if warnings:
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="saga-translator: Hermes state.db セッション集計"
    )
    parser.add_argument("db_path", help="path to Hermes state.db")
    parser.add_argument("--session", help="session ID or title to show detail")
    parser.add_argument("--last", type=int, default=5, help="show last N sessions (default: 5)")
    parser.add_argument(
        "--format", choices=("json", "text"), default="text",
        help="output format (default: text)",
    )
    args = parser.parse_args()

    # Check DB existence
    db_path_str = args.db_path
    try:
        with open(db_path_str, "rb") as _f:
            pass
    except FileNotFoundError:
        if args.format == "json":
            print(json.dumps({"error": f"database not found: {db_path_str}"}))
        else:
            print(f"error: database not found: {db_path_str}", file=sys.stderr)
        sys.exit(1)

    # Open read-only
    try:
        # Use URI for mode=ro on Windows
        import pathlib
        abs_path = str(pathlib.Path(db_path_str).resolve())
        uri = f"file:{abs_path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = 1")
        cursor = conn.cursor()
    except sqlite3.DatabaseError as e:
        if args.format == "json":
            print(json.dumps({"error": f"cannot open database: {e}"}))
        else:
            print(f"error: cannot open database: {e}", file=sys.stderr)
        sys.exit(1)

    # Schema check
    warnings = _check_schema(cursor)
    exit_code = 0
    if warnings:
        # Check if we are completely missing sessions
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='sessions'")
        if not cursor.fetchone():
            if args.format == "json":
                print(json.dumps({"error": "unsupported schema: sessions table not found",
                                  "warnings": warnings}))
            else:
                print("error: unsupported schema: sessions table not found", file=sys.stderr)
                for w in warnings:
                    print(f"warning: {w}", file=sys.stderr)
            conn.close()
            sys.exit(2)
        exit_code = 2

    try:
        if args.session:
            _detail_session(cursor, args.session, args.format)
        else:
            _list_sessions(cursor, args.last, args.format)
    except sqlite3.OperationalError as e:
        if args.format == "json":
            print(json.dumps({"error": str(e), "warnings": warnings if warnings else None}))
        else:
            print(f"error: {e}", file=sys.stderr)
            for w in warnings:
                print(f"warning: {w}", file=sys.stderr)
        conn.close()
        sys.exit(2)

    # Print schema warnings in text mode (for exit_code 2 situations)
    if warnings and exit_code == 2 and args.format == "text":
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)

    conn.close()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()