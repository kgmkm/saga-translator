#!/usr/bin/env python3
"""vocab.py — 辞書共通 API（phase7-taskP1 / P0-3）

拡張スキル（saga-translator-spicy 等）の ``references/vocab.toml``・
``house_style.toml``・作品の ``analysis/terms.toml`` を、一つの辞書 ``Vocab`` に
統合する。優先順位は **作品 terms ＞ house_style ＞ 拡張 vocab.toml** で、
同じ ``src`` は上位が勝つ。

用途:
- prepare（build_context.py）: 対象ブロックの原文に出現する語の訳語を差し込む
- commit（saga.py）: 残渣検査に渡す
  - fixed: 違反（原語形が残る・規定訳が無い）は commit 停止
  - candidates: 原語相当の語が残れば commit 停止（裁定記録 ``vocab:<src>`` で通過）
  - ja_ok: 検査から除外
- 原文字形索引: ``src`` / ``src_variants`` の両方の字形で引ける
- 許容別表記: 拡張 vocab.toml の任意フィールド ``dst_variants``（規定訳の許容別表記
  リスト。dst とあわせて完全一致で照合し、どれか1つが該当段落・ブロックにあれば合格）
- 辞書版: 拡張 vocab.toml の ``version`` を ``dict_version()`` として返す
  （``rulings.py`` の dict_version に接続する）

語彙データの形式は ``references/data-schema.md``「拡張語彙データの形式」が正本。
標準ライブラリのみ。
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load_terms, load_toml  # noqa: E402

__all__ = [
    "DEFAULT_KIND",
    "KINDS",
    "NO_DICT_VERSION",
    "Vocab",
    "VocabEntry",
    "VocabFinding",
    "check_draft",
    "house_style_paths",
    "load_vocab",
]

#: 語彙エントリの種別（references/data-schema.md が正本）
KINDS = ("fixed", "candidates", "ja_ok")

#: kind 未指定・未知のときの既定（残存で停止する保守側）
DEFAULT_KIND = "candidates"

#: 辞書版が無いときの値（rulings.NO_DICT_VERSION と同義）
NO_DICT_VERSION = "none"

#: 画像リンク。実ファイル名（原語表記）を保持するため検査から除く。
_IMAGE_LINK = re.compile(r"!\[[^\]]*\]\([^)]*\)")


# ── 探索先 ──────────────────────────────────────────────────────────────

def house_style_paths(work) -> list[Path]:
    """house_style.toml の共通探索先（#19）。

    探索先は ``<work>/_house/house_style.toml`` と work 親の
    ``<work.parent>/_house/house_style.toml`` の2箇所。prepare（build_context）と
    commit（saga）が同じ関数を使う（従来は work.parent.parent と work.parent で
    不一致だった）。存在するものだけを優先順（work → 親）で返す。
    """
    work = Path(work)
    out: list[Path] = []
    for p in (work / "_house" / "house_style.toml",
              work.parent / "_house" / "house_style.toml"):
        if p.is_file() and p not in out:
            out.append(p)
    return out


def _extension_vocab_paths() -> list[Path]:
    """拡張スキル配下の references/vocab.toml の探索（複数可）。

    - 自動探索: このスキルの兄弟ディレクトリ（saga-translator-*）の
      ``references/vocab.toml``
    - ``SAGA_VOCAB_PATH``: os.pathsep 区切りで追加指定（ファイル、または
      ``vocab.toml`` / ``references/vocab.toml`` を含むディレクトリ）
    """
    out: list[Path] = []
    skill_root = Path(__file__).resolve().parent.parent
    try:
        for p in sorted(skill_root.parent.glob("saga-translator-*/references/vocab.toml")):
            if p.is_file() and p not in out:
                out.append(p)
    except OSError:
        pass
    for raw in os.environ.get("SAGA_VOCAB_PATH", "").split(os.pathsep):
        raw = raw.strip()
        if not raw:
            continue
        p = Path(raw)
        if p.is_file():
            cand: Path | None = p
        elif p.is_dir():
            cand = None
            for rel in ("vocab.toml", "references/vocab.toml"):
                q = p / rel
                if q.is_file():
                    cand = q
                    break
            if cand is None:
                continue
        else:
            continue
        if cand not in out:
            out.append(cand)
    return out


# ── データ構造 ──────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VocabEntry:
    """統合辞書の1エントリ。

    - ``src``: 原語（原文字形。簡体字のまま）
    - ``dst``: 既定訳（fixed では規定訳）
    - ``kind``: fixed | candidates | ja_ok
    - ``variants``: src_variants（別字形・別表記。src を除く）
    - ``dst_variants``: dst_variants（規定訳の許容別表記。dst を除く）
    - ``origin``: terms | house_style | vocab（どの探索先から来たか）
    """
    src: str
    dst: str = ""
    kind: str = DEFAULT_KIND
    variants: tuple[str, ...] = ()
    dst_variants: tuple[str, ...] = ()
    note: str = ""
    origin: str = "vocab"

    @property
    def forms(self) -> tuple[str, ...]:
        """索引・照合に使う字形（src + variants。この順）。"""
        return (self.src,) + self.variants

    @property
    def dst_forms(self) -> tuple[str, ...]:
        """規定訳の照合に使う表記（dst + dst_variants。dst が無ければ dst_variants のみ）。"""
        return ((self.dst,) if self.dst else ()) + self.dst_variants


@dataclass(frozen=True)
class VocabFinding:
    """統合辞書の検査で見つかった1件。

    - ``kind``: fixed | candidates（ja_ok は出さない）
    - ``form``: 訳文に現れた字形（dst 欠落の違反では原文側の字形）
    - ``paragraph``: 対象の段落番号（ブロック全体の違反では 0）
    - ``detail``: 人間が読む説明（語と規定訳を含む）
    """
    kind: str
    entry: VocabEntry
    form: str
    paragraph: int
    detail: str


class Vocab:
    """統合辞書。優先順位つきのエントリ列と原文字形索引を持つ。"""

    def __init__(self, entries=None, versions=None, alias_forms=None):
        self._entries: list[VocabEntry] = [e for e in (entries or []) if e and e.src]
        self._versions: list[str] = [v for v in (versions or []) if v]
        # 原文字形索引: src / src_variants の両方で引ける。同じ字形を複数の
        # エントリが主張する場合は、優先順位の高い（先の）エントリが勝つ。
        self._index: dict[str, VocabEntry] = {}
        for e in self._entries:
            for form in e.forms:
                if form and form not in self._index:
                    self._index[form] = e
        # 優先順位で重複として落とした下位エントリの別字形も、勝者への
        # エイリアスとして残す（src が上書きされても別字形の索引を失わない）。
        for form, src in (alias_forms or ()):
            if form and form not in self._index:
                winner = next((x for x in self._entries if x.src == src), None)
                if winner is not None:
                    self._index[form] = winner

    @property
    def entries(self) -> list[VocabEntry]:
        """優先順位順のエントリ列（コピー）。"""
        return list(self._entries)

    def lookup(self, form: str) -> VocabEntry | None:
        """字形（src / src_variants）からエントリを引く。無ければ None。"""
        return self._index.get(form)

    def dict_version(self) -> str:
        """辞書の版（rulings の dict_version に接続）。無ければ "none"。"""
        return "+".join(self._versions) if self._versions else NO_DICT_VERSION

    def select_by_source(self, texts) -> list[VocabEntry]:
        """原文テキスト列に出現する語のエントリ（優先順）を返す（prepare 用）。

        src / src_variants のどの字形で出現しても選ぶ（別字形の経路）。
        """
        joined = "\n".join(str(t) for t in texts)
        out: list[VocabEntry] = []
        for e in self._entries:
            for form in e.forms:
                if form and form in joined:
                    out.append(e)
                    break
        return out


# ── 読み込み ────────────────────────────────────────────────────────────

def _norm_variants(raw, base: str) -> tuple[str, ...]:
    out: list[str] = []
    if isinstance(raw, list):
        for v in raw:
            if isinstance(v, str) and v and v != base and v not in out:
                out.append(v)
    return tuple(out)


def _entry_from_vocab_row(row) -> VocabEntry | None:
    """拡張 vocab.toml の [[vocab]] 1行からエントリを作る。不正なら None。"""
    if not isinstance(row, dict):
        return None
    src = row.get("src")
    if not isinstance(src, str) or not src:
        return None
    dst = row.get("dst")
    if not isinstance(dst, str):
        dst = ""
    kind = row.get("kind")
    if kind not in KINDS:
        kind = DEFAULT_KIND
    note = row.get("note")
    if not isinstance(note, str):
        note = ""
    return VocabEntry(src=src, dst=dst, kind=kind,
                      variants=_norm_variants(row.get("src_variants"), src),
                      dst_variants=_norm_variants(row.get("dst_variants"), dst),
                      note=note, origin="vocab")


def _entries_from_terms(work: Path) -> list[VocabEntry]:
    """作品 terms（analysis/terms.toml）を読む。kind は candidates（残存で停止）。"""
    out: list[VocabEntry] = []
    for t in load_terms(work).get("terms", []):
        if not isinstance(t, dict):
            continue
        src = t.get("term_src")
        if not isinstance(src, str) or not src:
            continue
        dst = t.get("term_ja")
        if not isinstance(dst, str):
            dst = ""
        out.append(VocabEntry(src=src, dst=dst, kind=DEFAULT_KIND, origin="terms"))
    return out


def _entries_from_house_style(work: Path, paths: list[Path]) -> list[VocabEntry]:
    """house_style.toml の goi ルール（approved / locked・適用範囲内）を読む。

    適用範囲は表示（build_context._fmt_house_style）と同じ基準:
    scope=global、または scope=work で target がこの作品。series / character は
    従来どおり対象外（#19 の表示側の課題。探索先の共通化のみ本関数の担当）。
    """
    work_id = Path(work).name
    out: list[VocabEntry] = []
    for p in paths:
        data = load_toml(p)
        if not isinstance(data, dict):
            continue
        for r in data.get("rules", []):
            if not isinstance(r, dict) or r.get("kind") != "goi":
                continue
            if r.get("status") not in ("approved", "locked"):
                continue
            scope = r.get("scope")
            if scope == "global":
                pass
            elif scope == "work" and r.get("target") == work_id:
                pass
            else:
                continue
            src = r.get("pattern")
            if not isinstance(src, str) or not src:
                continue
            dst = r.get("replacement") or r.get("preference") or ""
            if not isinstance(dst, str):
                dst = ""
            out.append(VocabEntry(src=src, dst=dst, kind=DEFAULT_KIND,
                                  origin="house_style"))
    return out


def load_vocab(work) -> Vocab:
    """作品 terms / house_style / 拡張 vocab.toml を統合した辞書を返す。

    優先順位: 作品 terms ＞ house_style（work 側 → 親側）＞ 拡張
    （自動探索 → SAGA_VOCAB_PATH）。同じ src は先に読んだ（上位の）エントリが勝つ。
    辞書版（dict_version）は拡張 vocab.toml の version を優先順に "+" で連結する。
    """
    work = Path(work)
    entries: list[VocabEntry] = []
    seen: set[str] = set()
    alias_forms: list[tuple[str, str]] = []

    def add_all(items) -> None:
        for e in items:
            if e is None or not e.src:
                continue
            if e.src in seen:
                # 重複は上位が勝つが、下位の別字形は勝者へのエイリアスに回す
                for form in e.forms:
                    if form and form != e.src:
                        alias_forms.append((form, e.src))
                continue
            seen.add(e.src)
            entries.append(e)

    add_all(_entries_from_terms(work))
    add_all(_entries_from_house_style(work, house_style_paths(work)))

    versions: list[str] = []
    for p in _extension_vocab_paths():
        data = load_toml(p)
        if not isinstance(data, dict):
            continue
        ver = data.get("version")
        if isinstance(ver, (str, int)) and not isinstance(ver, bool):
            s = str(ver)
            if s and s not in versions:
                versions.append(s)
        rows = data.get("vocab")
        if isinstance(rows, list):
            add_all(_entry_from_vocab_row(r) for r in rows)

    return Vocab(entries, versions=versions, alias_forms=alias_forms)


# ── 検査（commit の残渣検査） ───────────────────────────────────────────

def _iter_positions(text: str, needle: str):
    """text 内の needle の全出現位置（先頭文字の index）。needle は非空。"""
    start = 0
    while True:
        i = text.find(needle, start)
        if i < 0:
            return
        yield i
        start = i + 1


def _in_dst_span(pos: int, length: int, spans) -> bool:
    """出現 [pos, pos+length) が規定訳（dst / dst_variants）の出現範囲に完全に含まれるか。

    dst が原語形を含む語（例: 原語「合成語B」→ 規定訳「合成訳B」）でも、正しい
    訳語の内部を「原語形が残っている」と誤検知しないための判定。
    """
    return any(s <= pos and pos + length <= e for s, e in spans)


def _residue_detail(entry: VocabEntry, form: str) -> str:
    dst = entry.dst or "(未規定)"
    if entry.kind == "fixed":
        return f"「{form}」は規定訳「{dst}」で訳す（原語形が残っています）"
    return (f"「{form}」→ 既定訳「{dst}」（原語相当の語が残っています）\n"
            "既訳が混在する作品は rename_terms.py で統一してから")


def _allowed_dst_label(entry: VocabEntry) -> str:
    """規定訳の表示ラベル（許容別表記があれば併記）。"""
    if not entry.dst:
        return "規定訳" + "".join(f"『{v}』" for v in entry.dst_variants)
    if not entry.dst_variants:
        return f"規定訳『{entry.dst}』"
    alts = "".join(f"『{v}』" for v in entry.dst_variants)
    return f"規定訳『{entry.dst}』（別表記{alts}も可）"


def _fixed_dst_by_paragraph(entry: VocabEntry, src_items,
                            draft_paras: dict[int, str],
                            known: list[str]) -> list[VocabFinding]:
    """fixed の規定訳検査（段落単位）。

    原文段落 n に語（src / src_variants）が出ていて、対応する訳文段落 n に規定訳
    （dst / dst_variants のいずれか。完全一致）が無ければ違反。1段落に複数回出現
    しても検査は1回（段落ごとに高々1件）。原文・訳文とも画像リンク ``![...](...)``
    を除いたビューで判定し、known_terms に含まれる字形は原文側の出現として数えない
    （ブロック全体検査と同じ保護）。
    """
    dst_forms = entry.dst_forms
    out: list[VocabFinding] = []
    seen: set[int] = set()
    for n, text in src_items:
        try:
            num = int(n)
        except (TypeError, ValueError):
            continue
        if num in seen:
            continue
        probe = _IMAGE_LINK.sub("", str(text))
        hit = ""
        for form in entry.forms:
            if not form or any(t in form for t in known):
                continue
            if form in probe:
                hit = form
                break
        if not hit:
            continue
        seen.add(num)
        if any(s in draft_paras.get(num, "") for s in dst_forms):
            continue
        out.append(VocabFinding(
            kind="fixed", entry=entry, form=hit, paragraph=num,
            detail=(f"原文段落{num}の『{hit}』は{_allowed_dst_label(entry)}"
                    "で訳されていません")))
    return out


def check_draft(vocab: Vocab, items, src_text: str = "",
                known_terms=None, src_items=None) -> list[VocabFinding]:
    """訳文ドラフト（段落列）を統合辞書で検査する（純関数。裁定・I/O は呼び出し側）。

    ``items`` は ``(段落番号, 本文)`` の列（qa_translation.parse_draft と同じ形）。
    画像リンク ``![...](...)`` は実ファイル名（原語表記）を保持するため検査から除く。

    - fixed: 訳文に原語相当の語（src / src_variants）が残る、または原文に語が
      出ているのに規定訳（dst / dst_variants のいずれか。完全一致）が訳文に無い
      → 違反
      - ``src_items``（原文の ``(段落番号, 本文)`` 列）を渡すと、規定訳の検査は
        段落単位になる: 原文段落 n に語が出ていて、対応する訳文段落 n に規定訳が
        無ければ違反（段落は原文と1対1）。省略時（None）は従来どおりブロック
        全体で検査する（互換）
    - candidates: 訳文に原語相当の語が残る → 違反
    - ja_ok: 検査しない
    - ``known_terms``: 作品全体で意図的に残す語。含まれる字形は除外する
    """
    known = [str(t) for t in (known_terms or ()) if str(t)]
    body = "\n".join(_IMAGE_LINK.sub("", t) for _, t in items)
    findings: list[VocabFinding] = []

    # 訳文段落のビュー（画像リンク除去済み。段落番号 → 本文）。src_items がある
    # 場合の段落単位の規定訳検査に使う（重複した段落番号は連結して扱う）。
    draft_paras: dict[int, str] = {}
    if src_items is not None:
        for n, text in items:
            try:
                num = int(n)
            except (TypeError, ValueError):
                continue
            probe = _IMAGE_LINK.sub("", str(text))
            draft_paras[num] = (f"{draft_paras[num]}\n{probe}"
                                if num in draft_paras else probe)

    for entry in vocab.entries:
        if entry.kind == "ja_ok":
            continue
        if any(t in entry.src for t in known):
            continue
        dst_forms = entry.dst_forms
        residue_found = False
        for n, text in items:
            probe = _IMAGE_LINK.sub("", text)
            # 規定訳（dst / dst_variants）の出現範囲。その内部を「原語形が残る」と
            # 誤検知しない（許容別表記が原語形を含む場合も同様）。
            dst_spans = [(i, i + len(s)) for s in dst_forms
                         for i in _iter_positions(probe, s)]
            for form in entry.forms:
                if not form or any(t in form for t in known):
                    continue
                for pos in _iter_positions(probe, form):
                    if _in_dst_span(pos, len(form), dst_spans):
                        continue
                    findings.append(VocabFinding(
                        kind=entry.kind, entry=entry, form=form,
                        paragraph=int(n), detail=_residue_detail(entry, form)))
                    residue_found = True
                    break
        if entry.kind == "fixed" and dst_forms and not residue_found:
            if src_items is not None:
                # 段落単位: 原文段落ごとに規定訳の出現を強制する（表記ブレの検出）
                findings.extend(_fixed_dst_by_paragraph(
                    entry, src_items, draft_paras, known))
            elif any(form in src_text for form in entry.forms if form):
                if not any(s in body for s in dst_forms):
                    shown = entry.dst or "／".join(entry.dst_variants)
                    findings.append(VocabFinding(
                        kind="fixed", entry=entry, form="", paragraph=0,
                        detail=(f"原文の「{entry.src}」の訳語は規定訳「{shown}」"
                                "（訳文に規定訳が見つかりません）")))
    return findings
