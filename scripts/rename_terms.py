"""訳語変更の伝播。旧訳→新訳の置換候補を列挙し、確認のうえ適用する。

対象は output/ 配下の訳文（*_ja.md / *_zh.md）と、人物表 characters の
aliases（別名・愛称・原文の異表記。P0-4）の2箇所。

保護（#22 の契約）:
- 画像リンク ![alt](path) のパスは実ファイル名（＝原語表記）と一致させる
  必要があるため、リンク全体を置換から退避する。
- ルビの読みは壊さない。VFM ルビ {親文字|読み} は読み側だけを退避し、
  親文字は改名対象に残す。[[rb:…]] は親文字と読みの分割を機械で確定
  できないため全体を退避する（読みは別途確認）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    atomic_write_text,
    characters_toml_path,
    direction,
    load_toml,
    save_characters,
    save_json,
    target_suffix,
    utf8,
)

# 画像リンク全体（パスは原語表記のまま。置換しない）
_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
# [[rb:…]] は全体を保護（親文字と読みの分割は機械で確定しない）
_RB_RE = re.compile(r"\[\[rb:[^\]]*\]\]")
# VFM ルビ {親文字|読み}。読み側だけを保護する
_VFM_RE = re.compile(r"\{([^{}|]*)\|([^{}|]*)\}")


def _pick_sentinels(text: str) -> tuple[str, str]:
    """退避に使う開き・閉じの2文字を選ぶ（本文に含まれない PUA。#25 と同じ方式）。"""
    pool = [chr(c) for c in range(0xE000, 0xF8FF + 1) if chr(c) not in text]
    if len(pool) < 2:
        raise ValueError("置換の退避に使える PUA 文字が本文中に存在しません")
    return pool[0], pool[1]


class _Protector:
    """保護区間（画像リンク・[[rb:…]]・VFM ルビの読み）を退避して元へ戻す。"""

    def __init__(self, text: str) -> None:
        self.open, self.close = _pick_sentinels(text)
        self.tokens: list[str] = []

    def _placeholder(self, i: int) -> str:
        # 開き＋閉じ×個数＋開き。トークン同士が部分文字列にならない形にする
        return self.open + self.close * (i + 1) + self.open

    def _stash(self, value: str) -> str:
        self.tokens.append(value)
        return self._placeholder(len(self.tokens) - 1)

    def mask(self, text: str) -> str:
        out = _IMG_RE.sub(lambda m: self._stash(m.group(0)), text)
        out = _RB_RE.sub(lambda m: self._stash(m.group(0)), out)

        def _reading(m: "re.Match[str]") -> str:
            if not m.group(2):
                return m.group(0)
            return "{" + m.group(1) + "|" + self._stash(m.group(2)) + "}"

        return _VFM_RE.sub(_reading, out)

    def restore(self, text: str) -> str:
        for i, tok in enumerate(self.tokens):
            text = text.replace(self._placeholder(i), tok)
        return text


def _mask(text: str) -> tuple[str, _Protector]:
    p = _Protector(text)
    return p.mask(text), p


def count_replaceable(text: str, old: str) -> int:
    """保護区間を除いた、置換対象になる出現数。"""
    masked, _p = _mask(text)
    return masked.count(old)


def replace_protected(text: str, old: str, new: str) -> tuple[str, int, int]:
    """保護区間を退避して置換する。戻り値: (置換後テキスト, 置換数, 保護により除外した数)。"""
    masked, p = _mask(text)
    n = masked.count(old)
    if n:
        masked = masked.replace(old, new)
    return p.restore(masked), n, text.count(old) - n


def scan(work: Path, old: str) -> list[dict]:
    hits = []
    out_dir = work / "output"
    suf = target_suffix(direction(work)[1])
    for f in sorted(out_dir.rglob(f"*{suf}.md")):
        rel = f.relative_to(out_dir)
        text = f.read_text(encoding="utf-8")
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        n = 0
        protected = 0
        para_hits: list[int] = []
        multi = False
        for i, p in enumerate(paras):
            masked, _pr = _mask(p)
            k = masked.count(old)
            if k:
                n += k
                para_hits.append(i + 1)
                if k >= 2:
                    multi = True
            protected += p.count(old) - k
        if n or protected:
            hits.append({"file": str(rel), "total": n, "paragraphs": para_hits,
                         "multi_in_para": multi, "protected": protected})
    return hits


def scan_aliases(work: Path, old: str) -> list[dict]:
    """人物表 aliases（別名・愛称・原文の異表記）内の旧語を列挙する。"""
    p = characters_toml_path(work)
    if not p.exists():
        return []
    try:
        data = load_toml(p)
    except Exception:
        return []
    hits = []
    for c in data.get("characters") or []:
        if not isinstance(c, dict):
            continue
        aliases = c.get("aliases")
        if not isinstance(aliases, list):
            continue
        for a in aliases:
            if isinstance(a, str) and old in a:
                hits.append({"name": str(c.get("name_src") or c.get("name_ja") or ""),
                             "alias": a, "total": a.count(old)})
    return hits


def apply_alias_renames(work: Path, old: str, new: str) -> tuple[int, list[str]]:
    """人物表 aliases 内の旧語を置換して保存する。戻り値: (置換数, 対象人物名の列)。"""
    p = characters_toml_path(work)
    if not p.exists():
        return 0, []
    data = load_toml(p)
    changed: list[str] = []
    n = 0
    for c in data.get("characters") or []:
        if not isinstance(c, dict):
            continue
        aliases = c.get("aliases")
        if not isinstance(aliases, list):
            continue
        out = []
        hit = False
        for a in aliases:
            if isinstance(a, str) and old in a:
                n += a.count(old)
                out.append(a.replace(old, new))
                hit = True
            else:
                out.append(a)
        if hit:
            c["aliases"] = out
            changed.append(str(c.get("name_src") or c.get("name_ja") or ""))
    if n:
        save_characters(work, data)
    return n, changed


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="訳語変更の伝播")
    ap.add_argument("work")
    ap.add_argument("--term", required=True, help="旧訳語")
    ap.add_argument("--to", required=True, help="新訳語")
    ap.add_argument("--apply", action="store_true", help="実際に置換する(未指定は影響表示のみ)")
    args = ap.parse_args()

    work = Path(args.work)
    hits = scan(work, args.term)
    alias_hits = scan_aliases(work, args.term)
    report = {"term": args.term, "to": args.to, "applied": False,
              "files": hits, "aliases": alias_hits}
    total = sum(h["total"] for h in hits)
    alias_total = sum(h["total"] for h in alias_hits)
    if not hits and not alias_hits:
        print(f"[rename] 「{args.term}」の出力への出現なし(影響0)")
    else:
        print(f"[rename] 「{args.term}」→「{args.to}」: {total}箇所 / {len(hits)}ファイル"
              f" / aliases {alias_total}箇所")
        for h in hits:
            flag = " ※同一段落に複数(文脈要確認)" if h["multi_in_para"] else ""
            prot = f" / 保護{h['protected']}箇所(画像パス・ルビの読み)" if h["protected"] else ""
            print(f"  - {h['file']}: {h['total']}箇所{flag}{prot}")
        for h in alias_hits:
            print(f"  - aliases: {h['name']}: 「{h['alias']}」 {h['total']}箇所")
    if args.apply and (hits or alias_hits):
        for h in hits:
            if not h["total"]:
                continue
            f = work / "output" / h["file"]
            text = f.read_text(encoding="utf-8")
            new_text, n, _protected = replace_protected(text, args.term, args.to)
            if n:
                atomic_write_text(f, new_text)
        n_alias, _names = apply_alias_renames(work, args.term, args.to)
        report["applied"] = True
        report["alias_applied"] = n_alias
        print("[rename] 適用完了。文脈依存の箇所(※付き)は該当ブロックの再翻訳を検討すること")
    save_json(work / "logs" / "rename_last.json", report)


if __name__ == "__main__":
    main()
