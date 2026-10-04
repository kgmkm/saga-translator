"""saga-translator: Hermes state.db セッション集計スクリプト.

Usage:
    python3 scripts/tools/hermes_session_stats.py <state.db パス> [--session <id-or-title>] [--last N] [--format json|text]

集計項目:
    - 壁時計 / API コール数 / 1コールの平均出力トークン数
    - skill_view の呼び出し回数
    - 時間分類（下記の用語定義に従う）

時間分類の用語定義
------------------
本スクリプトのコード・出力・docstring で使う用語は次のとおり固定する。

- **人間待ち**: 人間への要求（質問・承認プロンプトの表示）から、人間の回答までの時間。
  すなわち、AI が人間の裁定を待って停止していた時間。
- **応答**: ある応答から次の応答までの時間（AI の処理時間。stream 再試行・圧縮・
  モデル処理を含む）。旧フィールド ``human_wait_secs`` が指していたのは実はこちら。
- さらに「応答」を内訳として **stream 再試行 / 承認 / 圧縮 / モデル処理 / 未分類** に分ける。

未分類について
--------------
分類の根拠がログから取れない区間は推測で埋めず、すべて **未分類** に落とす。
内訳の合計と壁時計の差は捨てずに「未分類」として明示出力する。すなわち不変条件

    人間待ち + （応答の内訳合計） + 未分類 == 壁時計

を常に満たす。

旧フィールド名の扱い
--------------------
``human_wait_secs`` は deprecated。意味は「応答」（AI の処理時間）であり、人間待ち
ではない。後方互換のため turn 出力に alias として残すが、新規の利用では使わないこと。
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

# 時間分類のバケット名（親が固定した用語）
BUCKET_HUMAN_WAIT = "human_wait"
BUCKET_STREAM_RETRY = "stream_retry"
BUCKET_APPROVAL = "approval"
BUCKET_COMPACTION = "compaction"
BUCKET_MODEL = "model_processing"
BUCKET_UNCLASSIFIED = "unclassified"


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

    # Fetch message rows once; reuse for turn analysis and time breakdown.
    msg_rows = _fetch_message_rows(cursor, sid)
    turn_analysis = _compute_turn_analysis(msg_rows)
    base["turn_analysis"] = turn_analysis

    # Time classification aggregate (人間待ち / 応答 / 壁時計 / 未分類)
    base["time_breakdown"] = _compute_time_breakdown(
        started_at, ended_at, msg_rows, turn_analysis
    )

    if fmt == "json":
        _print_json({"session": base, "warnings": warnings if warnings else None})
    else:
        _print_text_detail(base, turn_analysis, base["time_breakdown"], warnings)

    return warnings, base


def _fetch_message_rows(cursor, session_id: str) -> list[tuple]:
    """Fetch (role, timestamp, token_count, tool_name, display_kind, compacted).

    Optional columns that are absent from the schema are returned as NULL.
    Returns [] on any schema/permission problem so the caller classifies
    nothing (everything falls to 未分類) rather than guessing.
    """
    # 実際のテーブル列を直接見る（MESSAGE_COLS は基本列だけなので、
    # 分類用の display_kind / compacted はここで別途確認する）。
    try:
        cursor.execute("PRAGMA table_info(messages)")
        actual_cols = {row[1] for row in cursor.fetchall()}
    except sqlite3.OperationalError:
        return []

    if not {"session_id", "role", "timestamp"} <= actual_cols:
        return []

    def col(name: str) -> str:
        return name if name in actual_cols else "NULL"

    try:
        cursor.execute(
            f"SELECT role, timestamp, {col('token_count')}, {col('tool_name')}, "
            f"{col('display_kind')}, {col('compacted')} "
            "FROM messages WHERE session_id=? ORDER BY timestamp",
            (session_id,),
        )
        return cursor.fetchall()
    except sqlite3.OperationalError:
        # display_kind / compacted may not exist on older schemas: retry minimal.
        try:
            cursor.execute(
                "SELECT role, timestamp, token_count, tool_name, NULL, NULL "
                "FROM messages WHERE session_id=? ORDER BY timestamp",
                (session_id,),
            )
            return cursor.fetchall()
        except sqlite3.OperationalError:
            return []


def _compute_turn_analysis(rows: list[tuple]) -> list[dict]:
    """Compute per-turn latency from pre-fetched message rows.

    各 turn の ``response_secs`` は user の入力から次の assistant 応答までの
    時間、すなわち AI の処理時間（応答）である。旧 ``human_wait_secs`` と
    同じ値だが、意味を取り違えないよう改名した（deprecated alias を併記）。
    """
    turns: list[dict] = []
    i = 0
    while i < len(rows):
        role, ts, tkn, tname, dkind, cp = rows[i]
        if role == "user":
            # Find next assistant+tools block
            assistant_idx = None
            for j in range(i + 1, len(rows)):
                if rows[j][0] == "assistant":
                    assistant_idx = j
                    break
            if assistant_idx is not None:
                latency = rows[assistant_idx][1] - ts if rows[assistant_idx][1] > ts else 0.0
                # Count tool calls in this block (after assistant, before next user)
                tool_calls = 0
                block_out_tokens = 0
                for j in range(assistant_idx, len(rows)):
                    if rows[j][0] == "user":
                        break
                    if rows[j][3] is not None:  # tool_name present
                        tool_calls += 1
                    if rows[j][0] == "assistant" and rows[j][2] is not None:
                        block_out_tokens += rows[j][2]

                response_secs = round(latency, 1)
                turns.append({
                    "turn_index": len(turns),
                    "user_at": _fmt_ts(ts),
                    "response_start": _fmt_ts(rows[assistant_idx][1]),
                    # 「応答」= AI の処理時間（人間待ちではない）。
                    "response_secs": response_secs,
                    # deprecated: 旧名。意味は response_secs（応答）と同じ。
                    "human_wait_secs": response_secs,
                    "tool_calls_in_block": tool_calls,
                    "output_tokens": block_out_tokens,
                })
            i = assistant_idx + 1 if assistant_idx is not None else i + 1
        else:
            i += 1

    return turns


def _empty_breakdown() -> dict:
    return {
        "human_wait_secs": 0.0,
        "response_secs": 0.0,
        BUCKET_STREAM_RETRY: 0.0,
        BUCKET_APPROVAL: 0.0,
        BUCKET_COMPACTION: 0.0,
        BUCKET_MODEL: 0.0,
        BUCKET_UNCLASSIFIED: 0.0,
        "wall_clock_secs": None,
        "breakdown_total_secs": 0.0,
        "breakdown_total_with_unclassified_secs": 0.0,
        "reconciles_with_wall_clock": False,
        "notes": [],
    }


def _interval_ctx(rows: list[tuple], start_ts: float, end_ts: float) -> dict:
    """Collect log-evidence markers present in [start_ts, end_ts].

    ``end_ts`` is the timestamp of the last output in the interval, so its own
    marker (e.g. ``display_kind='failed_turn'`` or ``compacted=1``) must count;
    hence the inclusive upper bound.
    """
    ctx = {"failed_turn": False, "compacted": False, "has_model_activity": False}
    for role, ts, _tkn, _tool, display_kind, compacted in rows:
        if ts is None or not (start_ts <= ts <= end_ts):
            continue
        if display_kind == "failed_turn":
            ctx["failed_turn"] = True
        if compacted:
            ctx["compacted"] = True
        if role in ("assistant", "tool"):
            ctx["has_model_activity"] = True
    return ctx


def _classify_ai_interval(start_ts: float, end_ts: float, ctx: dict) -> str:
    """Classify one AI-processing interval by log evidence only.

    ``ctx`` carries the evidence markers found inside the interval:
    ``failed_turn`` (stream 再試行), ``compacted`` (圧縮), and whether any
    assistant/tool activity exists (モデル処理). ``clarify`` is handled by the
    caller as human wait, not here. Absent evidence → 未分類 (推測で分類しない).
    """
    if end_ts <= start_ts:
        return BUCKET_UNCLASSIFIED
    if ctx.get("failed_turn"):
        return BUCKET_STREAM_RETRY
    if ctx.get("compacted"):
        return BUCKET_COMPACTION
    if ctx.get("has_model_activity"):
        return BUCKET_MODEL
    return BUCKET_UNCLASSIFIED


def _compute_time_breakdown(
    started_at: float | None,
    ended_at: float | None,
    rows: list[tuple],
    turn_analysis: list[dict],
) -> dict:
    """Sum the time classification over a session, with an explicit residual.

    壁時計を次の互いに排他的な区間で覆い、根拠のあるものだけを名付ける。

    - **人間待ち**: 直近の出力（assistant/tool）のあと、人間の回答（user）まで。
      ``clarify``（承認・質問プロンプト）の直後の人間待ちは「承認」を内訳に持つ。
    - **応答**: user の入力から次の user まで（= AI が動いていた区間）を、
      ログの根拠に応じて stream 再試行 / 圧縮 / モデル処理 に振り分ける。
      区間内に ``clarify`` があれば、その呼び出しから次の user までは
      （人間が承認に答えるまでの時間なので）人間待ちに移す。
    - **未分類**: どのバケットにも根拠が無い残り。内訳合計 + 未分類 == 壁時計。
    """
    b = _empty_breakdown()

    if started_at is None or ended_at is None:
        b["notes"].append("wall clock unavailable (running session)")
        return b

    wall = max(ended_at - started_at, 0.0)
    b["wall_clock_secs"] = round(wall, 1)

    if not rows or any(r[1] is None for r in rows):
        # タイムスタンプが無ければ何も分類できない。すべて未分類。
        b[BUCKET_UNCLASSIFIED] = b["wall_clock_secs"]
        b["notes"].append("message timestamps missing; entire wall clock unclassified")
        b["breakdown_total_with_unclassified_secs"] = b["wall_clock_secs"]
        b["reconciles_with_wall_clock"] = True
        return b

    human_wait = 0.0
    approval_wait = 0.0

    # 1 turn = user 入力から次の user 入力まで。この区間の中を
    # [user, 最後の出力) = AI の処理（応答）と [最後の出力, 次の user) = 人間待ち
    # に割る。区間内で最後に現れる出力（assistant/tool）が AI の停止点。
    user_indices = [k for k, r in enumerate(rows) if r[0] == "user"]
    for ui, u_idx in enumerate(user_indices):
        seg_start = rows[u_idx][1]
        seg_end = (
            rows[user_indices[ui + 1]][1]
            if ui + 1 < len(user_indices)
            else None
        )
        if seg_end is None or seg_end <= seg_start:
            continue

        # 区間内の出力（assistant/tool）の最終時刻と、最後の clarify を探す。
        ai_end = seg_start  # AI が動き続けた最終時刻
        clarify_ts = None
        for k in range(u_idx, len(rows)):
            ts_k = rows[k][1]
            if ts_k is None or ts_k >= seg_end:
                break
            if rows[k][0] in ("assistant", "tool"):
                if ts_k > ai_end:
                    ai_end = ts_k
            if rows[k][3] == "clarify":
                clarify_ts = ts_k

        # 最後の出力から次の user までは人間が考えて答えるまでの時間 → 人間待ち。
        if ai_end < seg_end:
            human_gap = seg_end - ai_end
            human_wait += human_gap
            # そのうち clarify（承認・質問プロンプト）以降は承認待ち。
            if clarify_ts is not None and ai_end <= clarify_ts < seg_end:
                approval_wait += seg_end - clarify_ts

        if ai_end > seg_start:
            ctx = _interval_ctx(rows, seg_start, ai_end)
            bucket = _classify_ai_interval(seg_start, ai_end, ctx)
            b[bucket] += ai_end - seg_start

    b["human_wait_secs"] = round(human_wait, 1)
    b["approval_wait_secs"] = round(approval_wait, 1)

    response_total = (
        b[BUCKET_STREAM_RETRY] + b[BUCKET_APPROVAL]
        + b[BUCKET_COMPACTION] + b[BUCKET_MODEL]
    )
    b["response_secs"] = round(response_total, 1)
    b["breakdown_total_secs"] = round(b["human_wait_secs"] + response_total, 1)

    for key in (BUCKET_STREAM_RETRY, BUCKET_APPROVAL, BUCKET_COMPACTION, BUCKET_MODEL):
        b[key] = round(b[key], 1)

    # 未分類 = 壁時計 − (人間待ち + 応答内訳合計)。負にならないよう clamp。
    residual = b["wall_clock_secs"] - b["breakdown_total_secs"]
    if residual < 0:
        residual = 0.0
    b[BUCKET_UNCLASSIFIED] = round(residual, 1)

    b["breakdown_total_with_unclassified_secs"] = round(
        b["breakdown_total_secs"] + b[BUCKET_UNCLASSIFIED], 1
    )
    b["reconciles_with_wall_clock"] = (
        abs(b["breakdown_total_with_unclassified_secs"] - b["wall_clock_secs"]) < 0.05
    )
    return b


def _print_text_detail(
    base: dict,
    turn_analysis: list[dict],
    breakdown: dict,
    warnings: list[str],
) -> None:
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
        print("--- Turn-by-turn latency (応答 = AI の処理時間) ---")
        for t in turn_analysis:
            print(
                f"  Turn {t['turn_index']}: user@{t['user_at']} "
                f"→ response in {t['response_secs']}s "
                f"(tools={t['tool_calls_in_block']}, out_tkns={t['output_tokens']})"
            )
        print("")
        responses = [t["response_secs"] for t in turn_analysis]
        total_response = sum(responses)
        avg_response = round(total_response / len(responses), 1) if responses else 0.0
        print(f"  Total response time (応答): {_fmt_duration(total_response)}")
        print(f"  Avg response per turn: {avg_response}s")
        print("")

    if breakdown:
        print("--- Time classification ---")
        print(f"  Human wait (人間待ち): {_fmt_duration(breakdown.get('human_wait_secs'))}")
        print(f"  Response (応答): {_fmt_duration(breakdown.get('response_secs'))}")
        print(f"    - stream retry (stream 再試行): "
              f"{_fmt_duration(breakdown.get(BUCKET_STREAM_RETRY))}")
        print(f"    - approval (承認): {_fmt_duration(breakdown.get(BUCKET_APPROVAL))}")
        print(f"    - compaction (圧縮): {_fmt_duration(breakdown.get(BUCKET_COMPACTION))}")
        print(f"    - model processing (モデル処理): "
              f"{_fmt_duration(breakdown.get(BUCKET_MODEL))}")
        print(f"  Wall clock (壁時計): {_fmt_duration(breakdown.get('wall_clock_secs'))}")
        print(f"  Breakdown total (内訳合計): "
              f"{_fmt_duration(breakdown.get('breakdown_total_secs'))}")
        print(f"  Unclassified (未分類 = 壁時計 − 内訳合計): "
              f"{_fmt_duration(breakdown.get(BUCKET_UNCLASSIFIED))}")
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