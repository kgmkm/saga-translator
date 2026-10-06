"""訳語変更の伝播。旧訳→新訳の置換候補を列挙し、確認のうえ適用する。

対象は output/ 配下の訳文と、人物表 characters の aliases（別名・愛称・
原文の異表記。P0-4）の2箇所。

保護（#22 の契約）:
- 画像リンク ![alt](path) のパスは実ファイル名（＝原語表記）と一致させる
  必要があるため、リンク全体を置換から退避する。
- ルビの読みは壊さない。VFM ルビ {親文字|読み} は読み側だけを退避し、
  親文字は改名対象に残す。[[rb:…]] は親文字と読みの分割を機械で確定
  できないため全体を退避する（読みは別途確認）。

確定後の契約（P0-6・#22。--apply は次を同じ実行で行う）:
1. 正本（output/_work/done/*_numbered.md。無ければ確定ブロック出力
   output/<ch>/<bid><suffix>.md）の訳文側を置換する。
2. 対訳（*_parallel.md / 全文_対訳.md）の訳文側だけを置換する（原文ブロックは
   無傷。text_scope の平行範囲 API で側を判定する）。
3. 派生物（章訳文・全文訳文・対訳・合本）は書き換えない。再生成が必要な状態
   として manifest に derivatives_stale_at を記録する（再生成は assemble）。
4. 変更した段落を含むブロックの検査記録を stale にする（chapters_meta の
   stale_after_rename。draft_hash が変わるため）。status / review は stale を
   「最新・検収済み」と扱わない。
5. 改名で親文字が変わったルビは「要確認リスト」に出す（読みは自動で変えない）。
6. 改名前後の語・対象ブロック・段落を _work/renames.jsonl に追記する
   （監査記録。purge しない）。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    append_jsonl,
    atomic_write_text,
    block_meta,
    characters_toml_path,
    direction,
    load_chapters_meta,
    load_toml,
    mark_derivatives_stale,
    save_characters,
    save_chapters_meta,
    save_json,
    target_suffix,
    utf8,
)
import text_scope as ts  # noqa: E402

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


# ── 改名の対象（正本 / 対訳 / 派生物）の列挙 ────────────────────────────

def _rel(work: Path, p: Path) -> str:
    """work からの相対パス（表示・記録用。区切りは '/'）。"""
    try:
        return str(p.relative_to(work)).replace("\\", "/")
    except ValueError:
        return str(p).replace("\\", "/")


def _load_meta(work: Path) -> dict | None:
    """chapters_meta を読む（無ければ None）。"""
    try:
        return load_chapters_meta(work)
    except (FileNotFoundError, OSError):
        return None


def _block_index(meta: dict | None) -> dict[tuple[str, str], dict]:
    """(章, ブロック) → block 辞書 の索引。"""
    idx: dict[tuple[str, str], dict] = {}
    for ch in (meta or {}).get("chapters", []) or []:
        cid = ch.get("id")
        for b in ch.get("blocks", []) or []:
            if isinstance(cid, str) and isinstance(b, dict) and isinstance(b.get("id"), str):
                idx[(cid, b["id"])] = b
    return idx


def _done_files(work: Path) -> list[Path]:
    """正本（確定したブロックごとの番号付き訳文）のファイルを列挙する。"""
    d = work / "output" / "_work" / "done"
    if not d.is_dir():
        return []
    return sorted(d.glob("*_numbered.md"))


def _canonical_emitted_files(work: Path, meta: dict | None) -> list[tuple[str, str, Path]]:
    """確定ブロック出力（output/<ch>/<bid><suffix>.md）を (章, ブロック, パス) で列挙する。

    chapters_meta があればその blocks から拾い、無ければ output/<ch>/ 配下の
    訳文ファイルから拾う。章訳文・対訳は派生物なので含めない。
    """
    suf = target_suffix(direction(work)[1])
    out_dir = work / "output"
    found: list[tuple[str, str, Path]] = []
    if meta is not None:
        for ch in meta.get("chapters", []):
            cid = ch.get("id")
            if not isinstance(cid, str):
                continue
            for b in ch.get("blocks", []) or []:
                bid = b.get("id")
                if not isinstance(bid, str):
                    continue
                p = out_dir / cid / f"{bid}{suf}.md"
                if p.is_file():
                    found.append((cid, bid, p))
        return found
    for p in sorted(out_dir.glob(f"*/*{suf}.md")):
        if p.is_file():
            found.append((p.parent.name, p.name[: -len(f"{suf}.md")], p))
    return found


def _parallel_files(work: Path) -> list[Path]:
    """対訳ファイル（ブロック対訳・章対訳・全文対訳）を列挙する。"""
    out_dir = work / "output"
    files = sorted(out_dir.rglob("*_parallel.md"))
    full = out_dir / "全文_対訳.md"
    if full.is_file():
        files.append(full)
    return sorted(set(files))


def _derivative_files(work: Path) -> list[Path]:
    """派生物（章訳文・全文訳文）のファイルを列挙する。

    対訳は訳文側を更新する側なので含めない。合本は work の外
    （build_series_edition の出力先）なので、manifest の stale 記録だけで扱う。
    """
    out_dir = work / "output"
    suf = target_suffix(direction(work)[1])
    files = [p for p in sorted(out_dir.glob(f"*{suf}.md")) if p.is_file()]
    full = out_dir / "全文_訳文.md"
    if full.is_file():
        files.append(full)
    return files


def _emitted_paras(text: str, block: dict | None) -> list[tuple[int | None, str]]:
    """確定ブロック出力を (段落番号, 本文) に分ける。

    段落番号は chapters_meta の宣言（paras 範囲）と段落の対応から取る。
    段落数が宣言と一致しなければ推測せず None（不明）にする。
    """
    paras = ts._paragraphs(text)
    rng = (block or {}).get("paras")
    if (isinstance(rng, (list, tuple)) and len(rng) == 2
            and all(isinstance(x, int) for x in rng)):
        lo, hi = int(rng[0]), int(rng[1])
        if len(paras) == hi - lo + 1:
            return [(lo + i, p) for i, p in enumerate(paras)]
    return [(None, p) for p in paras]


def _side_hits(items: list[tuple[int | None, str]], old: str) -> tuple[int, int, list[int], bool]:
    """段落列の (置換対象数, 保護数, 該当段落番号, 同一段落に複数) を返す。"""
    n = 0
    prot = 0
    paras: list[int] = []
    multi = False
    for pn, body in items:
        masked, _pr = _mask(body)
        k = masked.count(old)
        if k:
            n += k
            if pn is not None:
                paras.append(pn)
            if k >= 2:
                multi = True
        prot += body.count(old) - k
    return n, prot, paras, multi


def _count_parallel_dst(text: str, old: str) -> tuple[int, int]:
    """対訳の訳文側だけの (置換対象数, 保護数)。原文側は数えない。"""
    n = 0
    prot = 0
    for r in ts.parallel_ranges(text):
        if r.side != ts.SIDE_DST:
            continue
        seg = text[r.start:r.end]
        masked, _pr = _mask(seg)
        k = masked.count(old)
        n += k
        prot += seg.count(old) - k
    return n, prot


def scan_targets(work: Path, old: str) -> dict:
    """改名の対象を種別ごとに列挙する（正本 / 対訳 / 派生物）。

    戻り値: {"canonical": [...], "parallel": [...], "derivatives": [...]}。
    各要素は {"file": work 相対, "total": 置換対象数, "protected": 保護数, ...}。
    canonical は blocks（ブロックキー・段落番号・同一段落の複数フラグ）を持つ。
    parallel は訳文側だけを数える。derivatives は書き換えない（再 assemble で更新）。
    """
    work = Path(work)
    meta = _load_meta(work)
    idx = _block_index(meta)
    canonical: list[dict] = []
    for p in _done_files(work):
        text = p.read_text(encoding="utf-8")
        parsed = ts._split_done_name(p.name)
        block = f"{parsed[0]}/{parsed[1]}" if parsed else ""
        n, prot, paras, multi = _side_hits(ts._numbered_items(text), old)
        if not (n or prot):
            continue
        canonical.append({"file": _rel(work, p), "kind": "done", "total": n,
                          "protected": prot, "multi": multi,
                          "blocks": [{"block": block, "paras": paras, "count": n}] if block else []})
    for cid, bid, p in _canonical_emitted_files(work, meta):
        text = p.read_text(encoding="utf-8")
        n, prot, paras, multi = _side_hits(_emitted_paras(text, idx.get((cid, bid))), old)
        if not (n or prot):
            continue
        canonical.append({"file": _rel(work, p), "kind": "emitted", "total": n,
                          "protected": prot, "multi": multi,
                          "blocks": [{"block": f"{cid}/{bid}", "paras": paras, "count": n}]})
    parallel: list[dict] = []
    for p in _parallel_files(work):
        text = p.read_text(encoding="utf-8")
        n, prot = _count_parallel_dst(text, old)
        if n or prot:
            parallel.append({"file": _rel(work, p), "total": n, "protected": prot})
    derivatives: list[dict] = []
    for p in _derivative_files(work):
        text = p.read_text(encoding="utf-8")
        masked, _pr = _mask(text)
        n = masked.count(old)
        if n:
            derivatives.append({"file": _rel(work, p), "total": n})
    return {"canonical": canonical, "parallel": parallel, "derivatives": derivatives}


# ── ルビ読みの要確認リスト ─────────────────────────────────────────────

def _ruby_check_items(text: str, old: str, new: str) -> list[dict]:
    """改名で親文字が変わるルビ（VFM）と、全体保護の [[rb:]] を要確認項目にする。

    読みは自動で変えない。VFM は親文字だけが置換されるため、新しい親文字と
    既存の読みの組（before → after）を確認できる形で返す。[[rb:]] は親文字と
    読みの分割を機械で確定できないため、置換されないことを明示して返す。
    画像リンク内・[[rb:]] 内の VFM は対象にしない。
    """
    spans = [(m.start(), m.end()) for m in _IMG_RE.finditer(text)]
    spans += [(m.start(), m.end()) for m in _RB_RE.finditer(text)]
    out: list[dict] = []
    for m in _RB_RE.finditer(text):
        if old in m.group(0):
            out.append({"kind": "rb", "before": m.group(0), "after": ""})
    for m in _VFM_RE.finditer(text):
        if any(s < m.end() and m.start() < e for s, e in spans):
            continue
        if m.group(1) and old in m.group(1):
            out.append({"kind": "vfm", "before": m.group(0),
                        "after": "{" + m.group(1).replace(old, new) + "|" + m.group(2) + "}"})
    return out


def _ruby_items(items: list[tuple[int | None, str]], old: str, new: str) -> list[dict]:
    """段落列からルビの要確認項目を集める（段落番号付き）。"""
    out: list[dict] = []
    for pn, body in items:
        for it in _ruby_check_items(body, old, new):
            out.append({**it, "para": pn})
    return out


def _dedupe_ruby(items: list[dict]) -> list[dict]:
    """同じルビ（種別・内容・位置）の重複を落とす。"""
    seen: set[tuple] = set()
    out: list[dict] = []
    for it in items:
        key = (it.get("kind"), it.get("before"), it.get("block"), it.get("para"))
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
    return out


# ── 確定後の伝播（正本・対訳の更新と stale 化） ────────────────────────

def _merge_block_change(changed: dict, block: str, paras: list[int], file: str) -> None:
    """ブロックごとの変更（段落・対象ファイル）をまとめる。"""
    rec = changed.setdefault(block, {"block": block, "paras": [], "files": []})
    for pn in paras:
        if pn not in rec["paras"]:
            rec["paras"].append(pn)
    if file not in rec["files"]:
        rec["files"].append(file)


def _parallel_block_key(p: Path, work: Path) -> str:
    """ブロック対訳（output/<ch>/<b>_parallel.md）ならブロックキー、他は空。"""
    try:
        rel = p.relative_to(work / "output")
    except ValueError:
        return ""
    parts = rel.parts
    if len(parts) == 2 and parts[1].endswith("_parallel.md"):
        return f"{parts[0]}/{parts[1][:-len('_parallel.md')]}"
    return ""


def _update_parallel(work: Path, old: str, new: str, ruby_items: list[dict]) -> list[dict]:
    """対訳（*_parallel.md / 全文_対訳.md）の訳文側だけを置換する。

    原文ブロック（先頭行が '>'）は範囲ごとそのまま残す。置換の前後で原文側の
    ハッシュ（text_scope.scope_hash, side=src）が変わらないことを検査し、
    変わった場合は内部不整合として停止する（原文を壊さない契約）。
    """
    report: list[dict] = []
    for p in _parallel_files(work):
        text = p.read_text(encoding="utf-8")
        ranges = ts.parallel_ranges(text)
        src_before = ts.scope_hash(text, ranges, side=ts.SIDE_SRC)
        block_key = _parallel_block_key(p, work)
        pieces: list[str] = []
        local_ruby: list[dict] = []
        pos = 0
        n = prot = 0
        for r in ranges:
            pieces.append(text[pos:r.start])
            seg = text[r.start:r.end]
            if r.side == ts.SIDE_DST:
                seg2, k, pr = replace_protected(seg, old, new)
                n += k
                prot += pr
                for it in _ruby_check_items(seg, old, new):
                    local_ruby.append({**it, "block": block_key, "para": None,
                                       "file": _rel(work, p)})
                seg = seg2
            pieces.append(seg)
            pos = r.end
        pieces.append(text[pos:])
        if not n:
            continue
        new_text = "".join(pieces)
        src_after = ts.scope_hash(new_text, ts.parallel_ranges(new_text), side=ts.SIDE_SRC)
        if src_after != src_before:
            raise RuntimeError(
                f"対訳の原文側ハッシュが変化しました（内部不整合）: {_rel(work, p)}")
        atomic_write_text(p, new_text)
        ruby_items.extend(local_ruby)
        report.append({"file": _rel(work, p), "total": n, "protected": prot,
                       "src_hash": src_before, "src_hash_after": src_after})
    return report


def apply_propagation(work: Path, old: str, new: str, when: str | None = None) -> dict:
    """改名の確定後契約: 正本と対訳の訳文側を置換し、派生物と検査記録を stale 化する。

    1) 正本（done の番号付き訳文と確定ブロック出力）の訳文側を置換
    2) 対訳（*_parallel.md / 全文_対訳.md）の訳文側だけを置換（原文側は無傷）
    3) 派生物（章訳文・全文訳文・対訳・合本）は書き換えず stale として記録
       （manifest の derivatives_stale_at。再生成は assemble）
    4) 変更した段落を含むブロックの検査記録を stale にする
       （chapters_meta の stale_after_rename）
    5) 改名で親文字が変わったルビを「要確認リスト」に集める（読みは自動で変えない）

    戻り値: {"changed", "canonical", "parallel", "stale", "changed_blocks",
    "ruby_check", "when"}。
    """
    work = Path(work)
    meta = _load_meta(work)
    idx = _block_index(meta)
    changed: dict[str, dict] = {}
    canonical_report: list[dict] = []
    ruby_items: list[dict] = []

    # 1) 正本: done（番号付き訳文）
    for p in _done_files(work):
        text = p.read_text(encoding="utf-8")
        parsed = ts._split_done_name(p.name)
        block = f"{parsed[0]}/{parsed[1]}" if parsed else ""
        n, prot, paras, multi = _side_hits(ts._numbered_items(text), old)
        if not n:
            continue
        new_text, n2, _pr = replace_protected(text, old, new)
        atomic_write_text(p, new_text)
        canonical_report.append({"file": _rel(work, p), "kind": "done", "block": block,
                                 "total": n2, "protected": prot, "paras": paras,
                                 "multi": multi})
        if block:
            _merge_block_change(changed, block, paras, _rel(work, p))
        for it in _ruby_items(ts._numbered_items(text), old, new):
            ruby_items.append({**it, "block": block, "file": _rel(work, p)})

    # 1b) 正本: 確定ブロック出力（done が無いブロックの正本）
    for cid, bid, p in _canonical_emitted_files(work, meta):
        text = p.read_text(encoding="utf-8")
        blk = idx.get((cid, bid))
        n, prot, paras, multi = _side_hits(_emitted_paras(text, blk), old)
        if not n:
            continue
        new_text, n2, _pr = replace_protected(text, old, new)
        atomic_write_text(p, new_text)
        block = f"{cid}/{bid}"
        canonical_report.append({"file": _rel(work, p), "kind": "emitted", "block": block,
                                 "total": n2, "protected": prot, "paras": paras,
                                 "multi": multi})
        _merge_block_change(changed, block, paras, _rel(work, p))
        for it in _ruby_items(_emitted_paras(text, blk), old, new):
            ruby_items.append({**it, "block": block, "file": _rel(work, p)})

    # 2) 対訳: 訳文側だけを置換（原文ブロックは無傷）
    parallel_report = _update_parallel(work, old, new, ruby_items)

    # 3)-4) 派生物と検査記録の stale 化
    stale = {"blocks": sorted(changed), "recorded": False, "at": ""}
    if changed or parallel_report:
        if changed and meta is not None:
            for key in changed:
                b = block_meta(meta, key)
                if b is not None:
                    b["stale_after_rename"] = True
            save_chapters_meta(work, meta)
        stale["at"] = mark_derivatives_stale(work, when=when)
        stale["recorded"] = True

    return {"changed": bool(canonical_report or parallel_report),
            "canonical": canonical_report, "parallel": parallel_report,
            "stale": stale, "changed_blocks": [changed[k] for k in sorted(changed)],
            "ruby_check": _dedupe_ruby(ruby_items),
            "when": when or stale["at"]}


# ── CLI ───────────────────────────────────────────────────────────────

def renames_log_path(work) -> Path:
    """改名の監査記録（_work/renames.jsonl。purge しない）。"""
    return Path(work) / "_work" / "renames.jsonl"


def load_pairs(path: Path) -> list[tuple[str, str]]:
    """'旧<TAB>新' の行を読む（P1-6。空行・# 始まりは無視）。

    タブが無い行は行番号つきの ValueError。旧・新のどちらかが空の行は読み飛ばす。
    """
    pairs: list[tuple[str, str]] = []
    for i, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" not in line:
            raise ValueError(f"{path}:{i}: タブ区切り '旧<TAB>新' が必要です")
        old, new = line.split("\t", 1)
        if old and new:
            pairs.append((old, new))
    return pairs


def _fmt_hit(h: dict) -> str:
    """正本ヒットの（ブロック・段落）表記。"""
    bits: list[str] = []
    for b in h.get("blocks", []):
        if not b.get("block"):
            continue
        ps = ", ".join(f"P{p}" for p in b.get("paras", []))
        bits.append((b["block"] + (" " + ps if ps else "")).strip())
    return "（" + " / ".join(bits) + "）" if bits else ""


def _fmt_ruby_item(it: dict) -> str:
    """要確認リストの1行。"""
    where = str(it.get("block") or "")
    if it.get("para"):
        where += f" P{it['para']}"
    where = f"（{where.strip()}）" if where.strip() else ""
    if it.get("kind") == "vfm":
        return f"{it['before']} → {it['after']}（読みの要確認）{where}"
    return f"{it['before']}（全体保護のため未変更。手当ての要確認）{where}"


def _run_one(work: Path, old: str, to: str, apply: bool) -> dict:
    """1ペア分の改名（影響表示と、--apply 時の適用。P1-6 の一括モードでも共用）。

    保護（画像パス・ルビ読み）と確定後の契約（正本・対訳の訳文側の更新、派生物と
    検査記録の stale 化、renames.jsonl への記録）は従来の1ペア処理のまま。
    戻り値は logs/rename_last.json に保存する report。
    """
    targets = scan_targets(work, old)
    alias_hits = scan_aliases(work, old)
    canon_total = sum(h["total"] for h in targets["canonical"])
    par_total = sum(h["total"] for h in targets["parallel"])
    deriv_total = sum(h["total"] for h in targets["derivatives"])
    alias_total = sum(h["total"] for h in alias_hits)
    report = {"term": old, "to": to, "applied": False,
              "files": scan(work, old), "aliases": alias_hits}

    if not (targets["canonical"] or targets["parallel"] or targets["derivatives"] or alias_hits):
        print(f"[rename] 「{old}」の出力への出現なし（変更なし。影響0）")
    else:
        print(f"[rename] 「{old}」→「{to}」: 正本 {canon_total}箇所 / "
              f"対訳(訳文側) {par_total}箇所 / 派生 {deriv_total}箇所 / aliases {alias_total}箇所")
        for h in targets["canonical"]:
            flag = " ※同一段落に複数(文脈要確認)" if h.get("multi") else ""
            prot = f" / 保護{h['protected']}箇所(画像パス・ルビ読み)" if h["protected"] else ""
            print(f"[rename]   正本: {h['file']}: {h['total']}箇所{flag}{prot} {_fmt_hit(h)}")
        for h in targets["parallel"]:
            prot = f" / 保護{h['protected']}箇所" if h["protected"] else ""
            print(f"[rename]   対訳(訳文側): {h['file']}: {h['total']}箇所{prot}（原文側は無傷）")
        for h in targets["derivatives"]:
            print(f"[rename]   派生: {h['file']}: {h['total']}箇所（書き換えない。再 assemble で更新）")
        for h in alias_hits:
            print(f"[rename]   aliases: {h['name']}: 「{h['alias']}」 {h['total']}箇所")

    if apply:
        ts_now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        prop = apply_propagation(work, old, to, when=ts_now)
        n_alias, alias_names = apply_alias_renames(work, old, to)
        changed = prop["changed"] or n_alias > 0
        report.update({
            "applied": changed,
            "alias_applied": n_alias,
            "canonical": prop["canonical"],
            "parallel": prop["parallel"],
            "stale": prop["stale"],
            "ruby_check": prop["ruby_check"],
        })
        if changed:
            if prop["changed"]:
                c_total = sum(h["total"] for h in prop["canonical"])
                p_total = sum(h["total"] for h in prop["parallel"])
                print(f"[rename] 適用: 正本 {c_total}箇所 / 対訳(訳文側) {p_total}箇所 を置換"
                      "（画像パス・ルビ読みは保護。原文側は無傷）")
            if n_alias:
                print(f"[rename] 適用: aliases {n_alias}箇所を置換")
            if prop["stale"]["recorded"]:
                if prop["stale"]["blocks"]:
                    print(f"[rename] stale: {', '.join(prop['stale']['blocks'])} の検査記録を stale 化"
                          f"（manifest に derivatives_stale_at={prop['stale']['at']}）")
                else:
                    print(f"[rename] stale: manifest に derivatives_stale_at={prop['stale']['at']} を記録")
            if prop["ruby_check"]:
                print("[rename] ルビ読みの要確認リスト（読みは自動で変えない）:")
                for it in prop["ruby_check"]:
                    print(f"[rename]   - {_fmt_ruby_item(it)}")
            else:
                print("[rename] ルビ読みの要確認リスト: なし")
            append_jsonl(renames_log_path(work), {
                "ts": ts_now,
                "term": old,
                "to": to,
                "canonical": prop["canonical"],
                "parallel": prop["parallel"],
                "blocks": prop["changed_blocks"],
                "ruby_check": prop["ruby_check"],
                "aliases": {"count": n_alias, "names": alias_names},
            })
            print(f"[rename] 監査記録: {renames_log_path(work)} に追記")
            print("[rename] 再 assemble が必要（改名により stale）。"
                  "章訳文・全文訳文・対訳・合本は再生成してください")
            print("[rename] 適用完了。文脈依存の箇所(※付き)は該当ブロックの再翻訳を検討すること")
        elif targets["canonical"] or targets["parallel"] or alias_hits:
            print("[rename] 書き換え対象（正本・対訳）に変更なし")
            for h in targets["derivatives"]:
                print(f"[rename]   派生に旧語が残っています（再 assemble で更新）: {h['file']}")
    return report


def main() -> int:
    utf8()
    ap = argparse.ArgumentParser(description="訳語変更の伝播")
    ap.add_argument("work")
    ap.add_argument("--term", default=None, help="旧訳語")
    ap.add_argument("--to", default=None, help="新訳語")
    ap.add_argument("--pairs-file", type=Path, default=None,
                    help="'旧<TAB>新' を並べた一括置換ファイル（空行・# 始まりは無視。P1-6）")
    ap.add_argument("--apply", action="store_true", help="実際に置換する(未指定は影響表示のみ)")
    args = ap.parse_args()

    work = Path(args.work)

    if args.pairs_file is not None:
        if args.term or args.to:
            print("[rename] エラー: --pairs-file と --term/--to は同時に指定できません",
                  file=sys.stderr)
            return 1
        try:
            pairs = load_pairs(args.pairs_file)
        except (OSError, ValueError) as e:
            print(f"[rename] エラー: {e}", file=sys.stderr)
            return 1
        if not pairs:
            print(f"[rename] ペアがありません（変更なし）: {args.pairs_file}")
            return 0
        print(f"[rename] 一括: {len(pairs)} ペア（{args.pairs_file}）"
              + ("（適用）" if args.apply else "（影響表示のみ）"))
        reports = [_run_one(work, old, new, args.apply) for old, new in pairs]
        save_json(work / "logs" / "rename_last.json",
                  {"pairs": reports,
                   "applied": any(bool(r.get("applied")) for r in reports)})
        return 0

    if not (args.term and args.to):
        print("[rename] エラー: --term と --to（または --pairs-file）が必要です",
              file=sys.stderr)
        return 1
    report = _run_one(work, args.term, args.to, args.apply)
    save_json(work / "logs" / "rename_last.json", report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
