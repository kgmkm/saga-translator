#!/usr/bin/env python3
"""align 検査の統合モジュール (P1-2)。

旧2本（align_check.py / check_align.py）との差分:
- 文字数比の逸脱は exit 1 から confirm（要 ack）に格下げ
- 重複マーカーは新規に fatal 化（旧ツールでは dict に上書きされて見逃せた）
- ack（確認記録）は rulings.py の裁定記録（rule_id="char_ratio"）として記録する。
  作品・章・ブロック・段落・原文ハッシュ・ドラフトハッシュ・検査設定の版が
  すべて一致する active な記録だけが confirm を抑制する（流用・陳腐化を遮断）。
  旧 _work/acks.jsonl は読み取り互換（一回きりの取り込み）。
- 削除は P1-6 で行う
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (
    LANG_PACKS,
    TomlSyntaxError,
    align_ratio_floor,
    dialogue_heads,
    direction,
    load_chapters_meta,
    load_paras,
    sha12,
    target_suffix,
)  # noqa: E402
from rulings import (  # noqa: E402
    MATCH_FIELDS,
    add_ruling,
    find_active_ruling,
    list_rulings,
    text_hash,
)


# ── データクラス ──────────────────────────────────────────────


@dataclass
class AlignFinding:
    severity: str  # "fatal" | "confirm"
    kind: str  # "missing_marker" | "duplicate_marker" | "count_mismatch"
    # | "dialogue_head" | "suffixed_marker" | "char_ratio"
    para: int | None  # 該当段落番号（全体問題は None）
    detail: str  # 人間が読む説明
    src_excerpt: str = ""
    dst_excerpt: str = ""


# ── 会話頭判定の例外（言語 pack）────────────────────────────────
# 構造タグ・擬音頭の例外は scripts/lang/<lang>.py の DIALOGUE_HEAD_EXCEPTIONS が正本。
# ここでは原文言語の pack を引くだけにして、例外の定義を1か所（pack）に集約する。


def dialogue_head_exceptions(src_lang: str) -> dict:
    """原文言語 pack の会話頭例外定義（DIALOGUE_HEAD_EXCEPTIONS）を返す。

    未知の言語は dialogue_heads の既定（zh→ja フォールバック）に合わせて zh を使う。
    """
    pack = LANG_PACKS.get(src_lang) or LANG_PACKS.get("zh")
    exc = getattr(pack, "DIALOGUE_HEAD_EXCEPTIONS", None) if pack is not None else None
    if not exc:
        raise RuntimeError(
            "会話頭例外の定義がありません: scripts/lang/ の pack に DIALOGUE_HEAD_EXCEPTIONS が必要"
        )
    return exc


# ── パース ────────────────────────────────────────────────────


def is_onomatopoeia_head(text: str, heads, exceptions: dict | None = None) -> bool:
    """短い引用符付きの語（多くは擬音）で始まり、残りが地の文の段落か。

    中国語の小説は擬音を “…” で括って地の文の頭に置くため、
    原文側の会話頭判定が fatal を誤爆する。引用が短く、閉じたあとの
    本文が十分長い場合だけ「擬音頭」とみなす。
    上限字数・閉じ文字・本文の倍率は言語 pack の例外定義が正本。
    """
    exc = exceptions if exceptions is not None else dialogue_head_exceptions("zh")
    t = text.lstrip()
    if not t.startswith(tuple(heads)):
        return False
    end = None
    for i, ch in enumerate(t[1:], start=1):
        if ch in exc["ono_head_closers"]:
            end = i
            break
    if end is None or end > exc["ono_head_max"]:
        return False
    rest = t[end + 1:]
    return len(rest) >= exc["ono_head_rest_factor"] * (end + 1)


def parse_numbered(text: str) -> list[tuple[int, str]]:
    """[Pn] マーカー付きドラフトを (番号, 本文) の列にする。重複もそのまま返す。"""
    items: list[tuple[int, str]] = []
    for part in re.split(r"(?m)^(?=\[P\d+\])", text):
        m = re.match(r"\[P(\d+)\]\s*(.*)", part, re.S)
        if m:
            items.append((int(m.group(1)), m.group(2).strip()))
    return items


def draft_hash(draft_path: Path) -> str:
    """ドラフトファイルの sha256（先頭12桁）。ack 記録の紐づけキー。"""
    return hashlib.sha256(draft_path.read_bytes()).hexdigest()[:12]


# ── ack（裁定記録）─────────────────────────────────────────────
# align の ack は rulings.py の裁定記録（全キー一致）が正本。旧 acks.jsonl は
# 読み取り互換として一回きり取り込み、新規の記録は旧版の読み取り用に併記する。

#: ack の裁定記録の rule_id（rulings.py）
ACK_RULE_ID = "char_ratio"


def _acks_path(work: Path) -> Path:
    """旧形式の ack 記録のパス（読み取り互換・併記用）。"""
    return Path(work) / "_work" / "acks.jsonl"


def _load_acks(work: Path) -> list[dict]:
    """旧 acks.jsonl を読む（壊れた行は警告して飛ばす）。"""
    p = _acks_path(work)
    if not p.exists():
        return []
    entries: list[dict] = []
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            print(f"[align] 警告: 旧 acks.jsonl の {i} 行目を読めません", file=sys.stderr)
            continue
        if isinstance(obj, dict):
            entries.append(obj)
        else:
            print(f"[align] 警告: 旧 acks.jsonl の {i} 行目が記録の形ではありません", file=sys.stderr)
    return entries


def config_version(work) -> str:
    """検査設定の版（裁定記録の dict_version）。

    house_style.toml（work/_house・親 _house）の中身から作る決定的な値。
    saga.py の _config_hash と同じ規則で、確定記録の config_hash と一致する。
    設定が変わると dict_version が変わり、古い裁定を陳腐化させる。
    """
    work = Path(work)
    parts: list[str] = []
    for label, path in (("work", work / "_house" / "house_style.toml"),
                        ("parent", work.parent / "_house" / "house_style.toml")):
        try:
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b""
        parts.append(f"{label}:{sha12(data)}")
    return sha12("|".join(parts).encode("utf-8"))


def _source_paras(work: Path, chapter: str) -> dict[int, str]:
    """原文段落 {n: text}。読めなければ空を返す（裁定記録は作れない）。"""
    try:
        data = load_paras(Path(work), chapter)
    except (FileNotFoundError, OSError, ValueError, TomlSyntaxError):
        return {}
    return {
        p["n"]: str(p.get("text", ""))
        for p in data.get("paras", [])
        if isinstance(p, dict) and isinstance(p.get("n"), int)
    }


def record_ack(work, chapter, block, paras: list[int], reason: str) -> dict:
    """ack を裁定記録（rulings）として記録する（全キー一致で有効）。

    キーは 作品・章・ブロック・段落・原文ハッシュ（原文段落）・ドラフトハッシュ
    （現在のドラフト）・dict_version（検査設定の版）。原文やドラフトが読めず
    裁定記録を作れない段落は、警告を出すだけで記録しない。
    旧版の読み取り互換のため、旧形式の1行を _work/acks.jsonl へも併記する
    （次回の読み取り時に重複なく取り込まれる）。
    戻り値は旧形式の entry（互換）。entry["rulings"] は記録した裁定の件数。
    """
    work = Path(work)
    _, dst_lang = direction(work)
    suf = target_suffix(dst_lang)
    draft_p = work / "output" / "_work" / f"{chapter}_{block}{suf}_numbered.md"
    dh = draft_hash(draft_p) if draft_p.exists() else "N/A"
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapter": chapter,
        "block": block,
        "paras": list(paras),
        "draft_hash": dh,
        "reason": reason,
    }

    # 裁定記録（正本）: 全キー一致の記録だけが有効
    src_texts = _source_paras(work, chapter)
    dv = config_version(work)
    recorded = 0
    for p in paras:
        src_text = src_texts.get(p)
        if src_text is None or dh == "N/A":
            print(
                f"[align] 警告: 裁定記録を作成できません（原文またはドラフトが"
                f"読めません）: {chapter}/{block} P{p}",
                file=sys.stderr,
            )
            continue
        add_ruling(
            work,
            work_id=work.name,
            chapter=chapter,
            block=block,
            paragraph=p,
            rule_id=ACK_RULE_ID,
            src_hash=text_hash(src_text),
            draft_hash=dh,
            dict_version=dv,
            rationale=reason,
            adjudicator="align.py ack",
            disposition="allow",
            status="active",
        )
        recorded += 1
    entry["rulings"] = recorded

    # 旧形式の併記（読み取り互換）
    p_path = _acks_path(work)
    p_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {k: entry[k] for k in ("ts", "chapter", "block", "paras",
                                    "draft_hash", "reason")}
    with p_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(legacy, ensure_ascii=False) + "\n")
    return entry


def _ruling_exists(records: list[dict], work_id: str, chapter: str, block: str,
                   paragraph: int, src_hash: str, draft_hash: str,
                   dict_version: str) -> bool:
    """全キー一致の裁定が既にあるか（active/revoked を問わない）。"""
    query = {
        "work_id": work_id, "chapter": chapter, "block": block,
        "paragraph": paragraph, "rule_id": ACK_RULE_ID, "src_hash": src_hash,
        "draft_hash": draft_hash, "dict_version": dict_version,
    }
    return any(all(r.get(k) == query[k] for k in MATCH_FIELDS) for r in records)


def migrate_legacy_acks(work) -> int:
    """旧 _work/acks.jsonl の ack を裁定記録へ一回きり取り込む。

    取り込みは冪等: 同じ全キーの裁定が既にあれば（active/revoked を問わず）
    作り直さない。原文が読めない等で取り込めない記録は、警告を出すだけで
    エラーにはしない（無効扱いにしない）。
    戻り値は取り込んだ裁定記録の件数。
    """
    work = Path(work)
    if not _acks_path(work).exists():
        return 0
    entries = _load_acks(work)
    if not entries:
        return 0

    imported = 0
    src_cache: dict[str, dict[int, str]] = {}
    dv = config_version(work)
    existing = list_rulings(work)
    for e in entries:
        chapter = str(e.get("chapter") or "")
        block = str(e.get("block") or "")
        dh = str(e.get("draft_hash") or "")
        paras = e.get("paras")
        if not chapter or not block or dh in ("", "N/A") or not isinstance(paras, list) or not paras:
            print(
                f"[align] 警告: 旧 ack を裁定記録へ取り込めません"
                f"（chapter/block/paras/draft_hash のいずれかが欠落）: {e}",
                file=sys.stderr,
            )
            continue
        src_texts = src_cache.setdefault(chapter, _source_paras(work, chapter))
        reason = str(e.get("reason") or "旧 acks.jsonl からの取り込み")
        ts = str(e.get("ts") or "")
        for raw_p in paras:
            try:
                p = int(raw_p)
            except (TypeError, ValueError):
                print(f"[align] 警告: 旧 ack の段落番号が不正: {raw_p!r}", file=sys.stderr)
                continue
            src_text = src_texts.get(p)
            if src_text is None:
                print(
                    f"[align] 警告: 旧 ack を裁定記録へ取り込めません"
                    f"（原文段落なし）: {chapter}/{block} P{p}",
                    file=sys.stderr,
                )
                continue
            src_h = text_hash(src_text)
            if _ruling_exists(existing, work.name, chapter, block, p, src_h, dh, dv):
                continue
            fields = {
                "work_id": work.name, "chapter": chapter, "block": block,
                "paragraph": p, "rule_id": ACK_RULE_ID, "src_hash": src_h,
                "draft_hash": dh, "dict_version": dv, "rationale": reason,
                "adjudicator": "legacy-migration", "disposition": "allow",
                "status": "active",
            }
            if ts:
                fields["decided_at"] = ts
            rec = add_ruling(work, **fields)
            existing.append(rec)
            imported += 1
    if imported:
        # 通知は stderr（stdout は check --json の JSON 出力に使う）
        print(f"[align] 旧 acks.jsonl から ack {imported} 件を裁定記録（rulings）へ取り込みました",
              file=sys.stderr)
    return imported


# ── 検査コア ──────────────────────────────────────────────────


def check_block(work, chapter, block, acks: list[dict] | None = None,
                config=None, use_rulings: bool = True) -> list[AlignFinding]:
    """chapters_meta + paras.toml + ドラフトを突き合わせ、fatal/confirm を返す。

    判定の詳細:
    - 期待番号は chapters_meta.toml の block の paras 範囲から
    - 会話頭判定は _lib.dialogue_heads(src, dst) を使用
    - 文字数比は _lib.align_ratio_floor(src, dst) を下限。原文25字以上に限定。
      上限は 3.0
    - confirm は rulings の裁定記録（rule_id="char_ratio"）が全キー一致
      （作品・章・ブロック・段落・原文ハッシュ・ドラフトハッシュ・設定版）の
      ときだけ抑制する（use_rulings=False は候補をそのまま返す）
    - acks（旧形式）は読み取り互換: (block, para, draft_hash) 一致でも抑制（併用）
    """
    work = Path(work)
    src_lang, dst_lang = direction(work)
    suf = target_suffix(dst_lang)
    src_heads, dst_heads = dialogue_heads(src_lang, dst_lang)
    src_exc = dialogue_head_exceptions(src_lang)
    ratio_floor = align_ratio_floor(src_lang, dst_lang)

    meta = load_chapters_meta(work)
    ch = next(
        (
            c
            for c in meta.get("chapters", [])
            if c.get("id") == chapter
            and any(b.get("id") == block for b in c.get("blocks", []))
        ),
        None,
    )
    if ch is None:
        return [AlignFinding("fatal", "count_mismatch", None, f"章 {chapter} にブロック {block} が見つかりません")]

    blk = next(b for b in ch["blocks"] if b["id"] == block)
    a, z = blk["paras"]
    expected = list(range(a, z + 1))

    paras_data = load_paras(work, chapter)
    para_map = {p["n"]: p["text"] for p in paras_data.get("paras", [])}

    draft_p = work / "output" / "_work" / f"{chapter}_{block}{suf}_numbered.md"
    if not draft_p.exists():
        return [AlignFinding("fatal", "count_mismatch", None, f"ドラフトが無い: {draft_p}")]

    dh = draft_hash(draft_p)
    text = draft_p.read_text(encoding="utf-8")
    items = parse_numbered(text)

    # 重複マーカー検出 (fatal)
    findings: list[AlignFinding] = []
    seen: dict[int, list[int]] = {}
    for idx, (n, _body) in enumerate(items):
        seen.setdefault(n, []).append(idx)
    for n, idxs in seen.items():
        if len(idxs) > 1:
            findings.append(AlignFinding(
                "fatal", "duplicate_marker", n,
                f"マーカー [P{n}] が {len(idxs)} 回出現",
            ))

    # 番号マップ（重複は最初の出現を取る）
    draft_map: dict[int, str] = {}
    for n, body in items:
        if n not in draft_map:
            draft_map[n] = body

    present = sorted(draft_map)

    # 接尾辞付きマーカー検出 (fatal)
    for m in re.finditer(r"(?m)^\[P(\d+)([^\]]*)\]", text):
        if m.group(2):
            n = int(m.group(1))
            findings.append(AlignFinding(
                "fatal", "suffixed_marker", n,
                f"接尾辞付きマーカー [P{n}{m.group(2)}]",
            ))

    # 段落数/番号の不一致 (fatal)
    if present != expected:
        missing = sorted(set(expected) - set(present))
        extra = sorted(set(present) - set(expected))
        detail_parts = []
        if missing:
            detail_parts.append(f"欠落={missing}")
        if extra:
            detail_parts.append(f"余剰={extra}")
        if missing or extra:
            findings.append(AlignFinding(
                "fatal", "count_mismatch", None,
                f"段落番号の不一致 期待{len(expected)}件/実際{len(present)}件 {' '.join(detail_parts)}",
            ))

    # ack の準備: (block, para, draft_hash) で index
    ack_index: set[tuple[str, int, str]] = set()
    if acks is not None:
        for a in acks:
            for p in a.get("paras", []):
                ack_index.add((a.get("block", block), int(p), a.get("draft_hash", "")))

    def is_acked(para: int) -> bool:
        return (block, para, dh) in ack_index

    # 会話頭不一致 (fatal)
    # 構造マーカー([newpage]・[chapter:]・[[rb:…]] 等)で始まる段落は対話判定から除外。
    # 原文側の会話頭に "[" が含まれるため、制御タグを会話と誤検出するのを防ぐ。
    # 例外の一覧・条件は原文言語の pack（DIALOGUE_HEAD_EXCEPTIONS）が正本。
    for n in expected:
        if n not in draft_map:
            continue
        src_text = para_map.get(n, "")
        dst_text = draft_map[n]
        if src_text.lstrip().startswith(tuple(src_exc["structure_prefixes"])):
            continue
        if is_onomatopoeia_head(src_text, src_heads, src_exc):
            continue
        if src_text.startswith(src_heads) and not dst_text.startswith(dst_heads):
            findings.append(AlignFinding(
                "fatal", "dialogue_head", n,
                f"原文は会話だが訳が会話になっていない",
                src_excerpt=src_text[:60], dst_excerpt=dst_text[:60],
            ))

    # 文字数比逸脱 (confirm)
    dv = config_version(work) if use_rulings else ""
    for n in expected:
        if n not in draft_map:
            continue
        src_text = para_map.get(n, "")
        dst_text = draft_map[n]
        if len(src_text) < 25:
            continue
        ratio = len(dst_text) / max(len(src_text), 1)
        if ratio < ratio_floor or ratio > 3.0:
            if is_acked(n):
                continue
            if use_rulings and find_active_ruling(
                work, rule_id=ACK_RULE_ID, chapter=chapter, block=block,
                paragraph=n, src_hash=text_hash(src_text), draft_hash=dh,
                dict_version=dv,
            ) is not None:
                continue
            findings.append(AlignFinding(
                "confirm", "char_ratio", n,
                f"文字数比 {ratio:.2f}（原文{len(src_text)}字→訳{len(dst_text)}字）",
                src_excerpt=src_text[:60], dst_excerpt=dst_text[:60],
            ))

    return findings


# ── CLI ────────────────────────────────────────────────────────


def cmd_check(args: argparse.Namespace) -> int:
    work = Path(args.work)
    # 旧 acks.jsonl は裁定記録へ取り込んでから照合する（読み取り互換）
    migrate_legacy_acks(work)
    findings = check_block(work, args.chapter, args.block)

    fatals = [f for f in findings if f.severity == "fatal"]
    confirms = [f for f in findings if f.severity == "confirm"]
    unacked = len(confirms)

    if args.json:
        print(json.dumps([{
            "severity": f.severity,
            "kind": f.kind,
            "para": f.para,
            "detail": f.detail,
            "src_excerpt": f.src_excerpt,
            "dst_excerpt": f.dst_excerpt,
        } for f in findings], ensure_ascii=False, indent=2))
        return 1 if (fatals or unacked) else 0

    # 裁定記録（rulings）で通過した confirm の件数（表示用。抑制前の候補との差）
    raw_confirms = [
        f for f in check_block(work, args.chapter, args.block, use_rulings=False)
        if f.severity == "confirm"
    ]
    # 人間向け出力
    print(f"[{args.block}] fatal {len(fatals)} 件 / confirm {unacked} 件"
          f"（裁定記録で通過 {len(raw_confirms) - unacked} 件）")
    for f in findings:
        tag = f.severity.upper()
        para_str = f"P{f.para}" if f.para is not None else "—"
        print(f"  {tag} {para_str} {f.kind}: {f.detail}")
        if f.src_excerpt:
            print(f"      原: {f.src_excerpt}")
        if f.dst_excerpt:
            print(f"      訳: {f.dst_excerpt}")

    if fatals or unacked:
        return 1
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    work = Path(args.work)
    paras = [int(x.strip().lstrip("Pp")) for x in args.para.split(",") if x.strip()]
    entry = record_ack(work, args.chapter, args.block, paras, args.reason)
    print(f"ack 記録: {entry['chapter']}/{entry['block']} paras={paras} hash={entry['draft_hash']}")
    print(f"  裁定記録(rulings): {entry['rulings']} 件 rule_id={ACK_RULE_ID}（全キー一致で有効）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="align 検査の統合モジュール")
    sub = ap.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check")
    p_check.add_argument("work")
    p_check.add_argument("--chapter", default="ch001")
    p_check.add_argument("--block", required=True)
    p_check.add_argument("--json", action="store_true")

    p_ack = sub.add_parser("ack")
    p_ack.add_argument("work")
    p_ack.add_argument("--chapter", default="ch001")
    p_ack.add_argument("--block", required=True)
    p_ack.add_argument("--para", required=True, help="確認済み段落番号（例 P1336,P1401）")
    p_ack.add_argument("--reason", required=True, help="確認理由")

    args = ap.parse_args()
    if args.command == "check":
        return cmd_check(args)
    elif args.command == "ack":
        return cmd_ack(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())