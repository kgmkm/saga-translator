"""ブロック横断状態の外部化台帳（_work/ledger.toml）。

レビュー提案1: ルビ適用の初出・用語の初出・使用済みオノマトペの申告を1ファイルに
蓄積し、prepare の翻訳コンテキストへ「消費済み」として注入する。analysis_edits.jsonl
と同じく purge 対象外で、assemble の後も work に残る。

- [[ruby_applied]]  : 確定訳文に現れた VFM {親|読み} の初出（キーは term+reading。
                      同一キーの2回目以降は記録しない）
- [[term_first_use]]: analysis/terms.toml の各 term_ja が確定訳文に初めて現れた位置
                      （既記録の語は追加しない）
- [[ono_used]]      : 翻訳者が申告した使用済みオノマトペ（saga.py ledger-ono。
                      本体に表が無いため自動抽出はしない）

更新は commit の確定時（saga._finalize_block）に当該ブロックの確定訳文だけを走査する
差分追記で、同一キーの再確定では重複しない（冪等）。

既知の制限（SKILL.md への文書化は親の担当）:
- rename_terms で親文字（訳語）を変更しても、台帳の term は古いまま残る
  （改名は追跡しない。台帳は commit 時の差分追記だけで更新される）。
- revise / 再確定でルビや用語が消えた場合も記録は削除しない（履歴として残す）。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import atomic_write_text, dump_toml, load_terms, load_toml  # noqa: E402

#: VFM ルビ {親文字|読み}。rename_terms と同じ形（読み側の保護対象と同じ書式）。
VFM_RE = re.compile(r"\{([^{}|]*)\|([^{}|]*)\}")

#: 旧形式のルビ 親{読み}（例: 戦鎧{せんがい}）。VFM ではないため VFM_RE では拾えず、
#: 台帳にも review の初出検査にも載らない（G11/T20）。親側は漢字連・読み側は仮名類に
#: 限定する（VFM は読み側の直前が `{` でなく `|` なのでこの形には当たらない）。
OLD_RUBY_RE = re.compile(r"([\u4e00-\u9fff々〆ヶ]{1,6})\{([ぁ-ゖァ-ヺー・]{1,20})\}")


def find_old_style_ruby(text: str) -> list[dict]:
    """旧形式ルビ 親{読み} を検出する（commit 時の警告用。自動変換はしない）。

    戻り値: ``[{"form": 原文どおり, "base": 親側, "reading": 読み側}]``。
    正常な VFM ``{親|読み}`` や、読みが仮名でない波括弧書きは対象外。
    """
    return [{"form": m.group(0), "base": m.group(1), "reading": m.group(2)}
            for m in OLD_RUBY_RE.finditer(text)]

LEDGER_VERSION = 1


def ledger_path(work) -> Path:
    """台帳のパス（<work>/_work/ledger.toml。analysis_edits.jsonl と同列）。"""
    return Path(work) / "_work" / "ledger.toml"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _empty() -> dict:
    """空の台帳構造（ファイルが無い・壊れて読めない場合の既定）。"""
    return {
        "ledger": {"version": LEDGER_VERSION, "updated_at": ""},
        "ruby_applied": [],
        "term_first_use": [],
        "ono_used": [],
    }


def _norm_para(v) -> int | None:
    """段落番号を整数に正す（不能なら None）。"""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _clean_entries(section: str, items) -> list[dict]:
    """既知キーだけを持つエントリ列に正す（キー順は固定。機械管理のファイル）。

    dump_toml は配列ofテーブルの後に裸のキー行を書くと、直前のテーブルへ
    吸収させてしまう（TOML の性質）。エントリに余計なキーを残さず、
    節の順序（ledger → ruby_applied → term_first_use → ono_used）も固定する。
    """
    out: list[dict] = []
    for x in items:
        if not isinstance(x, dict):
            continue
        if section == "ruby_applied":
            e: dict = {"term": str(x.get("term") or ""),
                       "reading": str(x.get("reading") or "")}
        elif section == "term_first_use":
            e = {"term": str(x.get("term") or "")}
        else:  # ono_used
            e = {"form": str(x.get("form") or "")}
        e["block"] = str(x.get("block") or "")
        para = _norm_para(x.get("paragraph"))
        if para is not None:
            e["paragraph"] = para
        if section == "ono_used":
            e["context"] = str(x.get("context") or "")
        out.append(e)
    return out


def load_ledger(work) -> dict:
    """台帳を読む。ファイルが無ければ空の構造を返す（後方互換）。

    壊れた TOML は _lib.load_toml が TomlSyntaxError を投げる（呼び出し側で扱う）。
    エントリは既知キーだけに正して返す（手編集で混ざったキーを持ち越さない）。
    """
    p = ledger_path(work)
    if not p.is_file():
        return _empty()
    data = load_toml(p)
    out = _empty()
    if isinstance(data, dict):
        meta = data.get("ledger")
        if isinstance(meta, dict):
            out["ledger"].update(meta)
        for key in ("ruby_applied", "term_first_use", "ono_used"):
            v = data.get(key)
            if isinstance(v, list):
                out[key] = _clean_entries(key, v)
    return out


def ledger_exists(work) -> bool:
    """台帳ファイルが存在するか。"""
    return ledger_path(work).is_file()


def ledger_counts(work) -> tuple[int, int, int] | None:
    """(ルビ消費, 用語初出, オノマトペ使用) の件数。台帳が無ければ None。"""
    if not ledger_path(work).is_file():
        return None
    d = load_ledger(work)
    return (len(d["ruby_applied"]), len(d["term_first_use"]), len(d["ono_used"]))


def save_ledger(work, data: dict) -> None:
    """台帳を保存する（updated_at を更新。キー順は表示の安定のため固定）。

    空の節は書かない。dump_toml は配列ofテーブルの後に裸のキー行（`key = []` 等）を
    書くと直前のテーブルへ吸収させるため、空配列の行は出さず、節の順序も
    （ledger → ruby_applied → term_first_use → ono_used）に固定する。
    """
    out: dict = {"ledger": {"version": LEDGER_VERSION, "updated_at": _now()}}
    for key in ("ruby_applied", "term_first_use", "ono_used"):
        items = _clean_entries(key, data.get(key) or [])
        if items:
            out[key] = items
    atomic_write_text(ledger_path(work), dump_toml(out))


def update_from_confirmed(work, block_key: str, items) -> dict:
    """確定訳文（(段落番号, 本文) の列）を走査し、台帳へ差分追記する（冪等）。

    追記するのは初出だけ:
    - ルビ: VFM {親|読み} をキー (term, reading) で全ブロック横断で初出のみ
    - 用語: analysis/terms.toml の term_ja ごとに、初めて現れた段落だけ

    戻り値: {"ruby": 追加数, "terms": 追加数}（追加が無ければファイルは触らない）。
    """
    data = load_ledger(work)
    ruby_keys = {(str(r.get("term") or ""), str(r.get("reading") or ""))
                 for r in data["ruby_applied"]}
    seen_terms = {str(t.get("term") or "") for t in data["term_first_use"]}

    term_jas: list[str] = []
    try:
        for t in (load_terms(work).get("terms") or []):
            if not isinstance(t, dict):
                continue
            tj = str(t.get("term_ja") or "").strip()
            if tj and tj not in term_jas:
                term_jas.append(tj)
    except Exception:
        term_jas = []  # terms を読めなくてもルビの記録は続ける

    ruby_added = 0
    for n, text in items:
        for m in VFM_RE.finditer(text):
            term, reading = m.group(1).strip(), m.group(2).strip()
            if not term:
                continue
            key = (term, reading)
            if key in ruby_keys:
                continue
            ruby_keys.add(key)
            data["ruby_applied"].append({
                "term": term,
                "reading": reading,
                "block": block_key,
                "paragraph": int(n),
            })
            ruby_added += 1

    term_added = 0
    for n, text in items:
        for tj in term_jas:
            if tj in seen_terms:
                continue
            if tj in text:
                seen_terms.add(tj)
                data["term_first_use"].append({
                    "term": tj,
                    "block": block_key,
                    "paragraph": int(n),
                })
                term_added += 1

    if ruby_added or term_added:
        save_ledger(work, data)
    return {"ruby": ruby_added, "terms": term_added}


def declare_ono(work, form: str, block: str, paragraph=None, context: str = "") -> dict:
    """オノマトペの使用済み申告を追記する（同一 form+block+para は重複しない）。

    戻り値: {"added": bool, "count": int}（count は追記後のオノマトペ総数）。
    """
    form = str(form or "").strip()
    block = str(block or "").strip()
    context = str(context or "").strip()
    para: int | None = int(paragraph) if paragraph is not None else None

    data = load_ledger(work)
    key = (form, block, para)
    for e in data["ono_used"]:
        if (str(e.get("form") or ""), str(e.get("block") or ""),
                e.get("paragraph")) == key:
            return {"added": False, "count": len(data["ono_used"])}

    entry: dict = {"form": form, "block": block}
    if para is not None:
        entry["paragraph"] = para
    entry["context"] = context
    data["ono_used"].append(entry)
    save_ledger(work, data)
    return {"added": True, "count": len(data["ono_used"])}
