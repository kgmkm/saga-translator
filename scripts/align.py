#!/usr/bin/env python3
"""align 検査の統合モジュール (P1-2)。

旧2本（align_check.py / check_align.py）との差分:
- 文字数比の逸脱は exit 1 から confirm（要 ack）に格下げ
- 重複マーカーは新規に fatal 化（旧ツールでは dict に上書きされて見逃せた）
- ack（確認記録）をドラフトハッシュに紐づけ。ハッシュ変更で無効化
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
    align_ratio_floor,
    dialogue_heads,
    direction,
    load_chapters_meta,
    load_paras,
    target_suffix,
)  # noqa: E402


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


# ── パース ────────────────────────────────────────────────────

# 構造マーカーで始まる段落は対話ではない（ルビ・挿絵タグ・章見出し・改ページ）。
# zh 原文の会話頭に "[" が含まれるため、制御タグを会話と誤検出するのを防ぐ。
_NON_DIALOGUE_HEADS = ("[[rb:", "[pixivimage:", "[chapter:", "[newpage]", "[uploadedimage:")


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


# ── ack ────────────────────────────────────────────────────────


def _acks_path(work: Path) -> Path:
    return Path(work) / "_work" / "acks.jsonl"


def _load_acks(work: Path) -> list[dict]:
    p = _acks_path(work)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def record_ack(work, chapter, block, paras: list[int], reason: str) -> dict:
    """現在のドラフトハッシュに紐づけて ack を _work/acks.jsonl に追記する。"""
    work = Path(work)
    src_lang, dst_lang = direction(work)
    suf = target_suffix(dst_lang)
    draft_p = work / "output" / "_work" / f"{chapter}_{block}{suf}_numbered.md"
    dh = draft_hash(draft_p) if draft_p.exists() else "N/A"
    entry = {
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "chapter": chapter,
        "block": block,
        "paras": paras,
        "draft_hash": dh,
        "reason": reason,
    }
    p = _acks_path(work)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# ── 検査コア ──────────────────────────────────────────────────


def check_block(work, chapter, block, acks: list[dict] | None = None,
                config=None) -> list[AlignFinding]:
    """chapters_meta + paras.toml + ドラフトを突き合わせ、fatal/confirm を返す。

    判定の詳細:
    - 期待番号は chapters_meta.toml の block の paras 範囲から
    - 会話頭判定は _lib.dialogue_heads(src, dst) を使用
    - 文字数比は _lib.align_ratio_floor(src, dst) を下限。原文25字以上に限定。
      上限は 3.0
    - acks は (block, para, draft_hash) が一致する confirm を抑制
    """
    work = Path(work)
    src_lang, dst_lang = direction(work)
    suf = target_suffix(dst_lang)
    src_heads, dst_heads = dialogue_heads(src_lang, dst_lang)
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
    for n in expected:
        if n not in draft_map:
            continue
        src_text = para_map.get(n, "")
        dst_text = draft_map[n]
        if src_text.lstrip().startswith(_NON_DIALOGUE_HEADS):
            continue
        if src_text.startswith(src_heads) and not dst_text.startswith(dst_heads):
            findings.append(AlignFinding(
                "fatal", "dialogue_head", n,
                f"原文は会話だが訳が会話になっていない",
                src_excerpt=src_text[:60], dst_excerpt=dst_text[:60],
            ))

    # 文字数比逸脱 (confirm)
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
            findings.append(AlignFinding(
                "confirm", "char_ratio", n,
                f"文字数比 {ratio:.2f}（原文{len(src_text)}字→訳{len(dst_text)}字）",
                src_excerpt=src_text[:60], dst_excerpt=dst_text[:60],
            ))

    return findings


# ── CLI ────────────────────────────────────────────────────────


def cmd_check(args: argparse.Namespace) -> int:
    work = Path(args.work)
    acks = _load_acks(work)
    findings = check_block(work, args.chapter, args.block, acks=acks)

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

    # 人間向け出力
    print(f"[{args.block}] fatal {len(fatals)} 件 / confirm {unacked} 件（ack 済み {len(confirms) - unacked} 件）")
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