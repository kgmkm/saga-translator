"""訳文の正本と「訳文範囲」の共通 API（P0-3・#23）。

背景（統合レビュー #23）:
- 「訳文だけ」を数える共通の手段が無く、自作スキャナが原文を混ぜて集計を
  3回やり直した。
- 対訳の原文は先頭行にしか `> [Pn]` が付かず、折り返しの継続行には `>` が
  無いため、行単位の除外では原文が訳文に残る。ここでは空行区切りの
  ブロック単位で原文ブロックを丸ごと落とす。
- TOML は行の正規表現ではなく、パース済みの構造から対象の欄を選ぶ
  （キーが `src` / `*_src` の値は原文側として除外する）。

訳文の正本は「確定したブロックごとの段落番号付き訳文」
（`output/_work/done/<ch>_<b><suffix>_numbered.md`）。無ければ確定ブロック出力
（`output/<ch>/<b><suffix>.md`）を使う。章訳文・全文訳文・対訳・parallel・
合本は派生物であり、この API からは読まない（検査・集計の対象にしない）。

範囲オブジェクト（TextRange）は原文側（src）と訳文側（dst）の区別を持ち、
`scope_hash()` で側ごとのハッシュを取れる。修正の前後で原文側のハッシュが
変わらないこと（原文を壊していないこと）の検証に使う。

標準ライブラリのみ。作品固有の情報は持たない。
"""
from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import direction, load_chapters_meta, load_paras, target_suffix  # noqa: E402

__all__ = [
    "SIDE_SRC",
    "SIDE_DST",
    "BlockText",
    "TextRange",
    "TextScopeError",
    "block_texts",
    "translation_only",
    "parallel_ranges",
    "parallel_translation_sides",
    "scope_hash",
    "toml_target_fields",
]

SIDE_SRC = "src"  # 原文側
SIDE_DST = "dst"  # 訳文側


class TextScopeError(ValueError):
    """訳文範囲を取れない（段落対応が壊れている等）ときに送出する。"""


@dataclass(frozen=True)
class BlockText:
    """正本の 1 段落分の訳文テキスト。

    chapter / block は chapters_meta の ID、para は段落番号（1 始まり）、
    text は訳文テキスト。origin は 'done'（_work/done/ の番号付き訳文）か
    'emitted'（output/<ch>/ の確定ブロック出力）。
    """

    chapter: str
    block: str
    para: int
    text: str
    origin: str = "done"


@dataclass(frozen=True)
class TextRange:
    """テキスト中の 1 ブロックの範囲（文字オフセット、start 含む・end 含まず）。

    side は SIDE_SRC（原文側）/ SIDE_DST（訳文側）。対訳は原文ブロックと訳文
    ブロックが交互に並ぶため、範囲ごとに区別を持つ。原文側のハッシュを修正の
    前後で比べる（scope_hash）といった検証に使う。
    para は先頭行の [Pn] から読めた段落番号（無ければ None）。
    """

    start: int
    end: int
    side: str
    para: int | None = None

    def slice(self, text: str) -> str:
        """この範囲が指す元テキストの部分を返す。"""
        return text[self.start:self.end]


# ── 空行区切りのブロック ──────────────────────────────────────────────

def _iter_blocks(text: str):
    """空行区切りのブロックを (start, end, lines) で返す。

    start / end は元テキストの文字オフセット（end は最終行の文字の直後。
    行末の改行は含まない）。lines は行末（LF / CRLF）を落とした各行。
    """
    start: int | None = None
    last_end = 0
    lines: list[str] = []
    off = 0
    for raw in text.split("\n"):
        end = off + len(raw)
        content_end = end - 1 if raw.endswith("\r") else end
        if raw.strip() == "":
            if start is not None:
                yield start, last_end, lines
            start, lines = None, []
        else:
            if start is None:
                start = off
                lines = []
            lines.append(raw[:-1] if raw.endswith("\r") else raw)
            last_end = content_end
        off = end + 1
    if start is not None:
        yield start, last_end, lines


def _block_side(first_line: str) -> str:
    """ブロック先頭行の役割。'>' 始まりは原文側、それ以外は訳文側。"""
    return SIDE_SRC if first_line.lstrip().startswith(">") else SIDE_DST


_PARA_RE = re.compile(r"\s*>?\s*\[P(\d+)\]")


def parallel_ranges(text: str) -> list[TextRange]:
    """対訳テキストの各ブロックを、原文側 / 訳文側の区別付きの範囲にする。

    ブロックは空行区切り。先頭行が `>`（空白を除いて）で始まるブロックは
    原文側（SIDE_SRC）、それ以外は訳文側（SIDE_DST）。折り返しの継続行は
    先頭行の役割を継ぐ（継続行に `>` が無くても行単位では判定しない）。
    範囲は元テキストの文字オフセットで、並びは出現順。
    """
    out: list[TextRange] = []
    for start, end, lines in _iter_blocks(text):
        first = lines[0]
        m = _PARA_RE.match(first)
        out.append(TextRange(start=start, end=end, side=_block_side(first),
                             para=int(m.group(1)) if m else None))
    return out


def parallel_translation_sides(parallel_text: str) -> str:
    """対訳テキストから訳文側だけを抽出して返す（#23 の本題）。

    原文ブロックは空行区切りのブロック単位で丸ごと落とす。原文の折り返し
    継続行は `>` を持たないが、ブロックの先頭行で役割を決めるため残らない。
    訳文ブロックはそのまま（行末の改行コードは LF に正規化）、ブロック間は
    空行 1 つで区切り、末尾に改行を 1 つ付ける。訳文ブロックが無ければ ""。
    """
    blocks = ["\n".join(lines) for _, _, lines in _iter_blocks(parallel_text)
              if _block_side(lines[0]) == SIDE_DST]
    if not blocks:
        return ""
    return "\n\n".join(blocks) + "\n"


def scope_hash(text: str, ranges: list[TextRange], side: str | None = None) -> str:
    """範囲オブジェクトで選んだ側のテキストの sha256（先頭12桁）。

    side=SIDE_SRC なら原文側、SIDE_DST なら訳文側、None なら全範囲。
    改行は LF に正規化し、範囲のテキストを LF で連結してからハッシュ化する。
    修正の前後で side=SIDE_SRC のハッシュが変わらないこと（原文側を壊して
    いないこと）の検証に使う。
    """
    parts = [text[r.start:r.end] for r in ranges if side is None or r.side == side]
    blob = "\n".join(parts).replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# ── 訳文の正本（確定ブロック） ────────────────────────────────────────

def _numbered_items(text: str) -> list[tuple[int, str]]:
    """番号付き訳文（`[Pn]` マーカー）を (段落番号, 本文) に分ける。"""
    items: list[tuple[int, str]] = []
    for part in re.split(r"(?m)^(?=\[P\d+\])", text):
        m = re.match(r"\[P(\d+)\]\s*(.*)", part, re.S)
        if m:
            items.append((int(m.group(1)), m.group(2).strip()))
    return items


def _paragraphs(text: str) -> list[str]:
    """空行区切りの段落に分ける（番号なしの確定ブロック出力用）。"""
    return ["\n".join(lines) for _, _, lines in _iter_blocks(text)]


def _done_path(work: Path, cid: str, bid: str, suf: str) -> Path:
    return work / "output" / "_work" / "done" / f"{cid}_{bid}{suf}_numbered.md"


def _emitted_path(work: Path, cid: str, bid: str, suf: str) -> Path:
    return work / "output" / cid / f"{bid}{suf}.md"


def _block_from_numbered(cid: str, bid: str, path: Path, origin: str) -> list[BlockText]:
    """番号付き訳文（正本）を BlockText の列にする。段落番号は [Pn] から。"""
    text = path.read_text(encoding="utf-8")
    return [BlockText(cid, bid, n, body, origin) for n, body in _numbered_items(text)]


# 対訳（`> [Pnnn] 原文` + 空行 + 訳文）のマーカー
_PAR_MARK = re.compile(r"(?m)^> \[P(\d+)\] ")


def _paragraphs_from_parallel(path: Path, src_map: dict[int, str]) -> dict[int, str]:
    """対訳ファイルから段落番号 → 訳文を取り出す（境界は [Pn] が正）。

    原文側は `chapters` の原文と照合して取り除く（原文が空なら最初の空行で分割）。
    段落内の空行は 1 つの改行に正規化する。
    """
    text = path.read_text(encoding="utf-8")
    parts = _PAR_MARK.split(text)
    out: dict[int, str] = {}
    for i in range(1, len(parts), 2):
        n = int(parts[i])
        body = parts[i + 1]
        src = (src_map.get(n) or "").strip()
        if src and body.startswith(src):
            dst = body[len(src):]
        else:
            seg = body.split("\n\n", 1)
            if len(seg) < 2:
                continue
            dst = seg[1]
        dst = "\n".join(ln.rstrip() for ln in dst.strip().splitlines() if ln.strip())
        if dst:
            out[n] = dst
    return out


def _recover_from_parallel(cid: str, bid: str, path: Path, lo: int, hi: int,
                           work) -> list[BlockText] | None:
    """段落内の空行で段落数が崩れたブロックを、対訳の [Pn] から組み直す。

    段落数が宣言と一致しなければ None（従来どおり呼び出し側が止める）。
    """
    par_p = path.parent / f"{bid}_parallel.md"
    if not par_p.is_file():
        return None
    if work is None:
        work = path.parent.parent.parent  # <work>/output/<ch>/<b>.md
    try:
        src_map = {int(p.get("n") or 0): str(p.get("text") or "")
                   for p in (load_paras(Path(work), cid).get("paras") or [])}
    except (FileNotFoundError, ValueError, KeyError):
        src_map = {}
    got = _paragraphs_from_parallel(par_p, src_map)
    need = set(range(lo, hi + 1))
    if set(got) != need:
        return None
    return [BlockText(cid, bid, n, got[n], "parallel") for n in range(lo, hi + 1)]


def _block_from_emitted(cid: str, bid: str, path: Path, block: dict,
                        work=None) -> list[BlockText]:
    """確定ブロック出力を BlockText の列にする。

    段落番号はファイルに無いため、chapters_meta の宣言（paras 範囲）と
    段落の対応から取る。段落数が宣言と一致しなければ対応が壊れているので
    推測せず停止する。
    """
    rng = block.get("paras")
    if (not isinstance(rng, (list, tuple)) or len(rng) != 2
            or not all(isinstance(x, int) for x in rng)):
        raise TextScopeError(
            f"{cid}/{bid}: 段落範囲（paras）が読めないため確定ブロック出力の"
            f"段落番号を対応できない: {path}")
    lo, hi = int(rng[0]), int(rng[1])
    paras = _paragraphs(path.read_text(encoding="utf-8"))
    if len(paras) != hi - lo + 1:
        # 段落内の空行が残った旧版の確定物。対訳の [Pn] から境界を取り直す。
        recovered = _recover_from_parallel(cid, bid, path, lo, hi, work)
        if recovered is not None:
            return recovered
        raise TextScopeError(
            f"{cid}/{bid}: 段落数が宣言（paras {lo}-{hi}）と一致しない"
            f"（{len(paras)} 段落）: {path}")
    return [BlockText(cid, bid, lo + i, p, "emitted") for i, p in enumerate(paras)]


def _split_done_name(name: str) -> tuple[str, str] | None:
    """`<ch>_<b><suffix>_numbered.md` の名前から (章ID, ブロックID) を読む。"""
    suffix = "_numbered.md"
    if not name.endswith(suffix):
        return None
    stem = name[: -len(suffix)]  # 例: ch001_b1_ja
    if "_" not in stem:
        return None
    base, _lang = stem.rsplit("_", 1)  # 末尾の言語接尾辞を落とす
    if "_" not in base:
        return None
    cid, bid = base.split("_", 1)
    return (cid, bid) if cid and bid else None


def _nat_key(s: str) -> str:
    """b2 < b10 のような自然順の比較キー（数字をゼロ埋めして文字列比較）。"""
    return re.sub(r"\d+", lambda m: m.group(0).zfill(9), s)


def block_texts(work) -> list[BlockText]:
    """正本（確定したブロックごとの訳文）の一覧を返す。

    優先順位: `output/_work/done/<ch>_<b><suffix>_numbered.md`（番号付き訳文。
    段落番号は [Pn] マーカーから）→ 無ければ `output/<ch>/<b><suffix>.md`
    （確定ブロック出力。段落番号は chapters_meta の段落範囲との対応から）。
    どちらも無いブロックは返さない。章訳文・全文訳文・対訳・parallel などの
    派生物は読まない。並びは章・ブロック・段落番号の自然順。
    chapters_meta が無い場合のみ done/ を直接走査する。
    """
    work = Path(work)
    suf = target_suffix(direction(work)[1])
    out: list[BlockText] = []
    try:
        meta = load_chapters_meta(work)
    except FileNotFoundError:
        meta = None

    if meta is not None:
        for ch in meta.get("chapters", []):
            cid = ch.get("id")
            if not isinstance(cid, str):
                continue
            for blk in ch.get("blocks", []):
                if not isinstance(blk, dict):
                    continue
                bid = blk.get("id")
                if not isinstance(bid, str):
                    continue
                done_p = _done_path(work, cid, bid, suf)
                if done_p.is_file():
                    out.extend(_block_from_numbered(cid, bid, done_p, "done"))
                    continue
                emit_p = _emitted_path(work, cid, bid, suf)
                if emit_p.is_file():
                    out.extend(_block_from_emitted(cid, bid, emit_p, blk, work))
    else:
        done_dir = work / "output" / "_work" / "done"
        for p in sorted(done_dir.glob("*_numbered.md")):
            parsed = _split_done_name(p.name)
            if parsed is None:
                continue
            cid, bid = parsed
            out.extend(_block_from_numbered(cid, bid, p, "done"))

    out.sort(key=lambda b: (_nat_key(b.chapter), _nat_key(b.block), b.para))
    return out


def translation_only(work) -> str:
    """全正本の訳文テキストを連結して返す（集計用。「訳文だけ」を数える）。

    段落は空行（LF 2つ）で区切る。正本が無ければ ""。派生物は含めない。
    """
    return "\n\n".join(b.text for b in block_texts(work))


# ── TOML（対象言語の欄） ──────────────────────────────────────────────

def _is_src_key(key: str) -> bool:
    """原文側のキーか（`src` / `*_src`）。"""
    return key == "src" or key.endswith("_src")


def toml_target_fields(data) -> list[str]:
    """パース済み TOML から対象言語の欄の値（文字列）を出現順に列挙する。

    キーが `src` / `*_src` の値は原文側として構造で除外する（行の正規表現は
    使わない。inline table の中も同じ規則で再帰する）。文字列以外の値は
    対象言語の欄ではないため含めない。
    """
    out: list[str] = []

    def _walk(obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not _is_src_key(str(k)):
                    _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)
        elif isinstance(obj, str):
            out.append(obj)

    _walk(data)
    return out
