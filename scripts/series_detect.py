"""連作候補の検出（ingest の補助）。

取り込む入力（ファイルまたはフォルダ）と、既存の作業フォルダ（`_saga/` 配下の
work）を突き合わせ、同一作者・連作の候補を探す。断定はしない——候補と根拠を
返すだけ。決定は `ingest` の `--series-from` / `--standalone` で記録する。

    from series_detect import find_candidates
    find_candidates("in/9990002-題名【2】.txt", "_saga")
    # [{"work_id": …, "path": …, "author": …, "title": …, "reason": …}, …]

根拠（複数一致時は「 / 」区切り）:
- 作者の一致（正規化して同一）／作者の近さ（表記ゆれ）
- タイトルの共通プレフィックス（続巻・同シリーズ）
- タイトルの近さ（表記ゆれ）

検出は補助機能。入力や既存 work が壊れていても ingest 本体を落とさない
（読めた範囲で判定し、読めなければそのファイル/ work を読み飛ばす）。
標準ライブラリのみ。ネットワーク不使用。
"""
from __future__ import annotations

import difflib
import re
import sys
import unicodedata
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    load_structured,
    parse_filename,
    parse_frontmatter,
    parse_novel_header,
    read_text_auto,
)

# work ディレクトリの目印（build_series_edition と同じ扱い）。
WORK_MARKERS = ("manifest.toml", "manifest.json", "chapters_meta.toml", "chapters_meta.json")
SRC_SUFFIXES = (".txt", ".md", ".epub")

MIN_PREFIX = 4          # タイトル共通プレフィックス: これ以上なら候補
MIN_CONTAINED = 2       # 短い側が丸ごとプレフィックス（＝包含）の場合の最短長
AUTHOR_NEAR_RATIO = 0.85
TITLE_NEAR_RATIO = 0.75

# 比較時に無視する空白・約物（NFKC 正規化後に残る半角側も含める）。
_STRIP_CHARS = set(
    " \t\r\n\u3000\u00a0"
    "()[]{}【】「」『』〈〉《》（）,，.。:：;；!！?？~〜～・…—–─―-=‐'\"“”‘’`/／\\|｜"
)


def work_name_for(fname: dict) -> str:
    """ingest.py と同じ規則で work ディレクトリ名を組み立てる（自己候補の除外に使う）。"""
    work_id = fname.get("novel_id") or re.sub(r"[^\w-]", "_", fname.get("title") or "")[:60] or "work"
    part = fname.get("part")
    return f"{work_id}-p{part}" if part else work_id


def _norm(s: str) -> str:
    """比較用の正規化: NFKC + casefold + 空白・約物の除去。"""
    s = unicodedata.normalize("NFKC", str(s or ""))
    return "".join(c for c in s.casefold() if c not in _STRIP_CHARS)


def _common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _author_reason(in_author: str, work_author: str) -> str | None:
    a, b = _norm(in_author), _norm(work_author)
    if not a or not b:
        return None
    if a == b:
        return f"作者の一致: {str(in_author).strip()}"
    if min(len(a), len(b)) >= 2 and (a in b or b in a):
        return f"作者の近さ（表記ゆれ）: {str(in_author).strip()} ≈ {str(work_author).strip()}"
    if difflib.SequenceMatcher(None, a, b).ratio() >= AUTHOR_NEAR_RATIO:
        return f"作者の近さ（表記ゆれ）: {str(in_author).strip()} ≈ {str(work_author).strip()}"
    return None


def _title_reason(in_title: str, work_title: str) -> str | None:
    a, b = _norm(in_title), _norm(work_title)
    if not a or not b:
        return None
    n = _common_prefix_len(a, b)
    shorter = min(len(a), len(b))
    if n >= MIN_PREFIX or (n == shorter and n >= MIN_CONTAINED):
        return f"タイトルの共通プレフィックス: 「{a[:n]}」"
    if difflib.SequenceMatcher(None, a, b).ratio() >= TITLE_NEAR_RATIO:
        return f"タイトルの近さ: 「{str(in_title).strip()}」≈「{str(work_title).strip()}」"
    return None


def _epub_meta(p: Path) -> dict:
    """EPUB の dc:title / dc:creator を best-effort で読む。"""
    try:
        with zipfile.ZipFile(p) as z:
            container = z.read("META-INF/container.xml").decode("utf-8", "replace")
            m = re.search(r'full-path="([^"]+)"', container)
            if not m:
                return {}
            opf = z.read(m.group(1)).decode("utf-8", "replace")
        meta: dict = {}
        t = re.search(r"<dc:title[^>]*>(.*?)</dc:title>", opf, flags=re.S | re.I)
        a = re.search(r"<dc:creator[^>]*>(.*?)</dc:creator>", opf, flags=re.S | re.I)
        if t and t.group(1).strip():
            meta["title"] = t.group(1).strip()
        if a and a.group(1).strip():
            meta["author"] = a.group(1).strip()
        return meta
    except Exception:
        # 検出は補助。壊れた EPUB で ingest を落とさない。
        return {}


def _file_entry(p: Path) -> dict:
    """1ファイルから (author, title, part, work_name) を推定する。読めなければ名前だけ。"""
    fname = parse_filename(p)
    title = str(fname.get("title") or "").strip()
    author = ""
    try:
        meta: dict | None
        if p.suffix.lower() == ".epub":
            meta = _epub_meta(p)
        else:
            text, _enc = read_text_auto(p)
            meta, _body = parse_frontmatter(text)
            if meta is None:
                meta, _body = parse_novel_header(text)
        if meta:
            title = str(meta.get("title") or title).strip()
            author = str(meta.get("author") or "").strip()
    except Exception:
        # 検出は補助。読めない入力はファイル名からの推定だけで扱う。
        pass
    return {"author": author, "title": title, "part": fname.get("part"), "work_name": work_name_for(fname)}


def _input_entries(src: Path) -> list[dict]:
    """入力（ファイル or フォルダ）から比較用のメタ一覧を作る。"""
    if src.is_dir():
        try:
            files = sorted(p for p in src.iterdir() if p.is_file() and p.suffix.lower() in SRC_SUFFIXES)
        except OSError:
            return []
    elif src.is_file():
        files = [src]
    else:
        return []
    return [_file_entry(p) for p in files]


def _load_if(path: Path) -> dict:
    """manifest / chapters_meta を読む。壊れていれば空辞書（候補判定では情報なし扱い）。"""
    try:
        return load_structured(path)
    except Exception:
        return {}


def _work_meta(d: Path) -> dict:
    """既存 work の work_id / title / author（manifest → chapters_meta の順で補完）。"""
    man = _load_if(d / "manifest")
    cm = _load_if(d / "chapters_meta")
    cwork = cm.get("work") if isinstance(cm.get("work"), dict) else {}
    title = str(man.get("title") or cwork.get("title") or "").strip()
    author = str(man.get("author") or cwork.get("author") or "").strip()
    work_id = str(man.get("work_id") or cwork.get("work_id") or d.name).strip()
    part = man.get("part") or cwork.get("part")
    if part and not work_id.endswith(f"-p{part}"):
        work_id = f"{work_id}-p{part}"
    return {"work_id": work_id, "title": title, "author": author}


def find_candidates(input_dir_or_file, saga_root) -> list[dict]:
    """入力と既存 work を突き合わせ、連作の候補を返す。

    各候補: {"work_id", "path", "author", "title", "reason"}。
    取り込み対象そのもの（同じ work 名になるディレクトリ）は候補から除く。
    """
    src = Path(input_dir_or_file)
    saga = Path(saga_root)
    entries = _input_entries(src)
    if not entries or not saga.is_dir():
        return []
    try:
        dirs = sorted((p for p in saga.iterdir() if p.is_dir()), key=lambda p: p.name)
    except OSError:
        return []
    exclude = {e["work_name"] for e in entries if e.get("work_name")}
    out: list[dict] = []
    for d in dirs:
        if d.name.startswith("_") or d.name in exclude:
            continue
        if not any((d / m).exists() for m in WORK_MARKERS):
            continue
        w = _work_meta(d)
        reasons: list[str] = []
        for e in entries:
            for r in (_author_reason(e["author"], w["author"]), _title_reason(e["title"], w["title"])):
                if r and r not in reasons:
                    reasons.append(r)
        if reasons:
            out.append({
                "work_id": w["work_id"],
                "path": str(d),
                "author": w["author"],
                "title": w["title"],
                "reason": " / ".join(reasons),
            })
    return out
