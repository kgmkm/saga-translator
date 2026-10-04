#!python3
"""rulings.py — 裁定記録のスキーマと保存 API（phase3-taskH / P2-3）

裁定（ack・語彙の許容・誤検出の除外など）を「ハッシュに紐づく記録」として
永続化する。保存先は `<work>/_work/rulings.json`。

設計方針:
- 標準ライブラリのみ。json + atomic 書き換えで冪等に扱う。
  TOML は使わない（既存の dump_toml に既知の順序特性があり、可変構造の
  本用途には不適）。
- 有効判定は「全キー一致」。作品・章・ブロック・段落・規則・原文ハッシュ・
  ドラフトハッシュ・辞書版のすべてが一致する active な記録だけが有効。
  どれか1つでも違えば無効（None）。これにより再実行や圧縮の後でも判断の
  やり直しが起きず、別章の同じ block や同じ本文への裁定流用、原文・検査設定の
  変更後の陳腐化を防ぐ。
- 失効は削除ではなく status=revoked への更新（履歴として残す）。

known_terms（作品全体の文字列除去）と局所の誤検出裁定は別のもの。
known_terms は house_style.toml の `residue.known_terms` のままとし、
rulings.json には混ぜない。

align ack の共通土台への移行・consumers（commit の語彙 candidates 等）の
接続は後続タスク（stage4/11）。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

# ── 定数 ──────────────────────────────────────────────────────────────

#: 記録1件の必須フィールド（スキーマ）
REQUIRED_FIELDS = (
    "work_id",
    "chapter",
    "block",
    "paragraph",
    "rule_id",
    "src_hash",
    "draft_hash",
    "dict_version",
    "rationale",
    "adjudicator",
    "decided_at",
    "disposition",
    "status",
)

#: 有効判定に使う一致キー（すべて一致で有効）
MATCH_FIELDS = (
    "work_id",
    "chapter",
    "block",
    "paragraph",
    "rule_id",
    "src_hash",
    "draft_hash",
    "dict_version",
)

#: disposition の許容値
VALID_DISPOSITIONS = frozenset({"allow", "reject", "accept_candidate"})

#: status の許容値
VALID_STATUSES = frozenset({"active", "revoked"})

#: dict_version が無い場合の既定値
NO_DICT_VERSION = "none"

#: ハッシュの桁数
HASH_LEN = 12


class RulingError(ValueError):
    """裁定記録のスキーマ違反。"""


# ── ユーティリティ ────────────────────────────────────────────────────

def _now_utc() -> str:
    """ISO8601(UTC) の現在時刻。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_text(text: str) -> str:
    """段落の正規化（改行・前後空白の除去）。

    原文・ドラフトのハッシュは正規化後のテキストから取る。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").strip()


def text_hash(text: str) -> str:
    """正規化したテキストの sha256 先頭12桁。"""
    norm = normalize_text(text)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:HASH_LEN]


def rulings_path(work: Path | str) -> Path:
    """`<work>/_work/rulings.json` のパス。"""
    return Path(work) / "_work" / "rulings.json"


def _empty_store() -> dict:
    return {"version": 1, "rulings": []}


def _load(work: Path | str) -> dict:
    """rulings.json を読む。無ければ空ストア。"""
    path = rulings_path(work)
    if not path.is_file():
        return _empty_store()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("rulings"), list):
        return _empty_store()
    return data


def _save(work: Path | str, data: dict) -> None:
    """rulings.json を atomic に書き換える。"""
    path = rulings_path(work)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _validate(fields: dict) -> None:
    """必須フィールドと値域を検証する。違反は RulingError。"""
    missing = [k for k in REQUIRED_FIELDS if k not in fields]
    if missing:
        raise RulingError(f"必須フィールドが欠落: {', '.join(missing)}")

    for k in ("work_id", "chapter", "block", "rule_id", "src_hash",
              "draft_hash", "rationale", "adjudicator", "decided_at",
              "disposition", "status"):
        v = fields.get(k)
        if not isinstance(v, str) or not v:
            raise RulingError(f"{k} は空でない文字列である必要があります")

    p = fields.get("paragraph")
    if isinstance(p, bool) or not isinstance(p, int):
        raise RulingError("paragraph は整数である必要があります")

    dv = fields.get("dict_version")
    if dv is not None and not isinstance(dv, str):
        raise RulingError("dict_version は文字列である必要があります")

    if fields["disposition"] not in VALID_DISPOSITIONS:
        raise RulingError(
            f"disposition が不正: {fields['disposition']!r} "
            f"(許容: {sorted(VALID_DISPOSITIONS)})")
    if fields["status"] not in VALID_STATUSES:
        raise RulingError(
            f"status が不正: {fields['status']!r} "
            f"(許容: {sorted(VALID_STATUSES)})")


# ── 公開 API ──────────────────────────────────────────────────────────

def add_ruling(work: Path | str, **fields) -> dict:
    """裁定記録を1件追加して永続化する。

    必須フィールドが欠けていたら RulingError で拒否する。
    dict_version 省略時は 'none'。decided_at 省略時は現在時刻。
    戻り値は保存した記録の dict。
    """
    if "dict_version" not in fields or fields["dict_version"] is None:
        fields["dict_version"] = NO_DICT_VERSION
    if not fields.get("decided_at"):
        fields["decided_at"] = _now_utc()

    _validate(fields)

    record = {k: fields[k] for k in REQUIRED_FIELDS}

    data = _load(work)
    data["rulings"].append(record)
    _save(work, data)
    return record


def find_active_ruling(
    work: Path | str,
    rule_id: str,
    chapter: str,
    block: str,
    paragraph: int,
    src_hash: str,
    draft_hash: str,
    dict_version: str = NO_DICT_VERSION,
    work_id: str | None = None,
) -> dict | None:
    """全キー一致の active な記録だけを返す。無ければ None。

    一致キー: work_id・chapter・block・paragraph・rule_id・src_hash・
    draft_hash・dict_version のすべて。どれか1つでも違えば無効（None）。
    work_id 省略時は work ディレクトリ名を使う。
    """
    query = {
        "work_id": work_id if work_id is not None else Path(work).name,
        "chapter": chapter,
        "block": block,
        "paragraph": paragraph,
        "rule_id": rule_id,
        "src_hash": src_hash,
        "draft_hash": draft_hash,
        "dict_version": dict_version or NO_DICT_VERSION,
    }
    for rec in _load(work)["rulings"]:
        if rec.get("status") != "active":
            continue
        if all(rec.get(k) == query[k] for k in MATCH_FIELDS):
            return dict(rec)
    return None


def revoke_ruling(
    work: Path | str,
    rule_id: str,
    chapter: str,
    block: str,
    paragraph: int,
    src_hash: str,
    draft_hash: str,
    dict_version: str = NO_DICT_VERSION,
    work_id: str | None = None,
) -> None:
    """全キー一致の active な記録を status=revoked に更新する。

    記録は削除せず履歴として残す。該当が無ければ何もしない。
    work_id 省略時は work ディレクトリ名を使う。
    """
    query = {
        "work_id": work_id if work_id is not None else Path(work).name,
        "chapter": chapter,
        "block": block,
        "paragraph": paragraph,
        "rule_id": rule_id,
        "src_hash": src_hash,
        "draft_hash": draft_hash,
        "dict_version": dict_version or NO_DICT_VERSION,
    }
    data = _load(work)
    changed = False
    for rec in data["rulings"]:
        if rec.get("status") != "active":
            continue
        if all(rec.get(k) == query[k] for k in MATCH_FIELDS):
            rec["status"] = "revoked"
            changed = True
    if changed:
        _save(work, data)


def list_rulings(work: Path | str) -> list[dict]:
    """保存されている全記録（active/revoked 混在）を追加順で返す。"""
    return [dict(r) for r in _load(work)["rulings"]]