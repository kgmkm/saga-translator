"""入力ファイル(TXT/MD/EPUB)の取り込み → ワークスペース生成。
画像は output/images/ に置き、訳文は常に `images/…` で参照する。"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    atomic_write_text, guess_lang, parse_filename, parse_novel_header,
    save_structured, split_paras, utf8,
)

IMG_REF = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
XHTML_TAG = re.compile(r"<[^>]+>")
UPLOADED_IMAGE = re.compile(r"\[uploadedimage:(\d+)\]", re.I)
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
# pixiv の [newpage] を行区切り、[chapter:] を独立段落に正規化する
NEWPAGE = re.compile(r"\n?[ \t]*\[newpage\][ \t]*\n?")


def normalize_pixiv_markers(text: str) -> str:
    """pixiv の [newpage]・[chapter:] を段落境界として書き直す。"""
    text = NEWPAGE.sub("\n\n[newpage]\n\n", text)
    text = re.sub(r"\n(?=\[chapter:)", "\n\n", text)
    text = re.sub(r"(?m)^(\[chapter:[^\]]*\])\n(?=\S)", lambda m: m.group(1) + "\n\n", text)
    return text
# 旧配置(../images)も含めて images/<file> 参照を images/<file> に正規化する
MD_IMAGE_SRC = re.compile(r"!\[([^\]]*)\]\((?:\.\./)*images/([^)]+)\)")


def images_dir_of(work: Path) -> Path:
    """画像置き場。output/ 内に固定。訳文からの相対は常に images/。"""
    return Path(work) / "output" / "images"


def retarget_md_images(text: str) -> str:
    """`(../)*images/<file>` を `images/<file>` に正規化。"""
    return MD_IMAGE_SRC.sub(lambda m: f"![{m.group(1)}](images/{m.group(2)})", text)


def image_search_dirs(src_dir: Path, work: Path) -> list[Path]:
    """原文と同じ場所、その下の image/ images/、output/images/。"""
    out: list[Path] = []
    seen: set[Path] = set()
    for d in (src_dir, src_dir / "image", src_dir / "images", images_dir_of(work)):
        try:
            r = d.resolve()
        except OSError:
            continue
        if r in seen:
            continue
        seen.add(r)
        out.append(d)
    return out


def find_image_for_id(image_id: str, search_dirs: list[Path]) -> Path | None:
    """ファイル名に ID が数字として含まれる画像。部分一致の誤爆を避ける。"""
    token = re.compile(rf"(^|[^0-9]){re.escape(image_id)}([^0-9]|$)")
    hits: list[Path] = []
    for d in search_dirs:
        if not d.is_dir():
            continue
        try:
            names = list(d.iterdir())
        except OSError:
            continue
        for p in names:
            if not p.is_file() or p.suffix.lower() not in IMAGE_EXT:
                continue
            if token.search(p.name):
                hits.append(p)
    if not hits:
        return None
    rank = {".png": 0, ".webp": 1, ".jpg": 2, ".jpeg": 2, ".gif": 3}
    hits.sort(key=lambda p: (rank.get(p.suffix.lower(), 9), p.name))
    return hits[0]


def rewrite_uploaded_images(
    text: str, images_dir: Path, search_dirs: list[Path]
) -> tuple[str, list[str], list[str]]:
    """[uploadedimage:ID] を ![](images/…) に置換。無ければタグを残す。
    戻り値: (本文, コピーしたパス, 見つからなかったID)"""
    copied: list[str] = []
    missing: list[str] = []
    seen_missing: set[str] = set()

    def repl(m: re.Match) -> str:
        iid = m.group(1)
        src_img = find_image_for_id(iid, search_dirs)
        if src_img is None:
            if iid not in seen_missing:
                seen_missing.add(iid)
                missing.append(iid)
            return m.group(0)
        images_dir.mkdir(parents=True, exist_ok=True)
        dest = images_dir / src_img.name
        if not dest.exists():
            shutil.copy2(src_img, dest)
        copied.append(str(dest))
        return f"![](images/{dest.name})"

    return UPLOADED_IMAGE.sub(repl, text), copied, missing


def missing_image_notice(ids: list[str]) -> str:
    if not ids:
        return ""
    sample = ids[0]
    listed = ", ".join(ids)
    return (
        f"原文に [uploadedimage:…] があったが、対応ファイルが見つかりませんでした: {listed}\n"
        f"output/images/ に、ファイル名へそのIDを含む画像を入れてください"
        f"（例: {sample}.png や 作品ID-{sample}-題名.png）。"
        "入れたあと assemble を再実行すると、残っているタグを埋め込みます。"
    )


def relink_markdown_files(work: Path) -> list[str]:
    """訳文に残る [uploadedimage:ID] を output/images/ から埋め込む。未解決IDを返す。"""
    from _lib import atomic_write_text
    work = Path(work)
    images_dir = images_dir_of(work)
    search = image_search_dirs(work, work)
    out = work / "output"
    files: list[Path] = []
    if out.exists():
        files.extend(p for p in out.rglob("*.md") if p.is_file())
    still: list[str] = []
    seen: set[str] = set()
    for f in files:
        text = f.read_text(encoding="utf-8")
        if not UPLOADED_IMAGE.search(text):
            continue
        new, _, missing = rewrite_uploaded_images(text, images_dir, search)
        for iid in missing:
            if iid not in seen:
                seen.add(iid)
                still.append(iid)
        if new != text:
            atomic_write_text(f, new)
    return still


def collect_md_images(text: str, images_dir: Path, src_dir: Path) -> tuple[str, list[str], list[str]]:
    """MDの画像参照を処理。ローカル相対パスのみコピーして参照を書き換える。
    リモートURLは取得しない。戻り値: (書き換え後テキスト, コピーした画像, リモートURL)"""
    images_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    remote: list[str] = []

    def repl(m: re.Match) -> str:
        alt, ref = m.group(1), m.group(2).strip()
        if "://" in ref:
            remote.append(ref)
            return m.group(0)
        p = Path(ref)
        if not p.is_absolute():
            p = (src_dir / ref).resolve()
        if p.exists():
            dest_name = p.name
            dest = images_dir / dest_name
            if not dest.exists():
                shutil.copy2(p, dest)
            copied.append(str(dest))
            return f"![{alt}](images/{dest_name})"
        return m.group(0)  # 見つからない参照はそのまま

    new_text = IMG_REF.sub(repl, text)
    return new_text, copied, remote


def epub_extract(path: Path, images_dir: Path) -> tuple[str, dict]:
    """EPUBから本文(段落)とメタ+画像を抽出。"""
    ns = {"c": "urn:oasis:names:tc:opendocument:package:container",
          "o": "http://www.idpf.org/2007/opf", "dc": "http://purl.org/dc/elements/1.1/"}
    images_dir.mkdir(parents=True, exist_ok=True)
    meta: dict = {}
    paras: list[str] = []
    with zipfile.ZipFile(path) as z:
        container = ET.fromstring(z.read("META-INF/container.xml"))
        rootfile = container.find(".//c:rootfile", ns)
        opf_path = rootfile.get("full-path")
        opf = ET.fromstring(z.read(opf_path))
        meta["title"] = (opf.findtext(".//dc:title", default="", namespaces=ns) or "").strip()
        meta["author"] = (opf.findtext(".//dc:creator", default="", namespaces=ns) or "").strip()
        manifest_items = {it.get("id"): it.get("href") for it in opf.findall(".//o:item", ns)}
        spine = [manifest_items[ir.get("idref")] for ir in opf.findall(".//o:itemref", ns)
                 if ir.get("idref") in manifest_items]
        base_dir = Path(opf_path).parent
        for href in spine:
            p = (base_dir / href).as_posix() if "." in str(base_dir) else href
            try:
                html = z.read(p).decode("utf-8", errors="replace")
            except KeyError:
                continue
            for tag in ("script", "style"):
                html = re.sub(rf"<{tag}[^>]*>.*?</{tag}>", "", html, flags=re.S | re.I)
            for pm in re.finditer(r"<(?:p|div)[^>]*>(.*?)</(?:p|div)>", html, re.S | re.I):
                t = XHTML_TAG.sub("", pm.group(1)).strip()
                if t:
                    paras.append(t)
        for name in z.namelist():
            low = name.lower()
            if low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                dest = images_dir / Path(name).name
                if not dest.exists():
                    dest.write_bytes(z.read(name))
    return "\n\n".join(paras), meta


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="入力の取り込み")
    ap.add_argument("input", help="TXT/MD/EPUBファイル")
    ap.add_argument("-o", "--out", default=None, help="ワークスペース親ディレクトリ(既定: 入力の隣の _saga/)")
    ap.add_argument("--dst", default=None, help="訳文言語(既定: 原語から推定 zh→ja / ja→zh)")
    args = ap.parse_args()

    src = Path(args.input).resolve()
    if not src.exists():
        raise SystemExit(f"入力が見つかりません: {src}")
    parent = Path(args.out).resolve() if args.out else src.parent / "_saga"

    fname = parse_filename(src)
    work_id = fname.get("novel_id") or re.sub(r"[^\w-]", "_", fname["title"])[:60] or "work"
    part = fname.get("part")
    work_name = f"{work_id}-p{part}" if part else work_id
    work = parent / work_name

    with timed_stage(work, "ingest"):
        _stage_impl(work, src, parent, args, work_id, part)


def _stage_impl(work: Path, src: Path, parent: Path, args: argparse.Namespace,
                work_id: str, part: str) -> None:

    meta: dict = {"source_path": str(src), "work_id": work_id, "part": part}
    images_dir = images_dir_of(work)
    copied: list[str] = []
    remote: list[str] = []
    missing_ids: list[str] = []

    suffix = src.suffix.lower()
    if suffix == ".epub":
        body, epub_meta = epub_extract(src, images_dir)
        meta.update({k: v for k, v in epub_meta.items() if v})
        body_text = body
        text = body_text
    else:
        from _lib import read_text_auto
        text, enc = read_text_auto(src)
        meta["encoding"] = enc
        if suffix == ".md":
            hdr, body_text = parse_novel_header(text)
            if hdr:
                meta.update({k: v for k, v in hdr.items() if k != "tags" or True})
            else:
                body_text = text
        else:
            hdr, body_text = parse_novel_header(text)
            if hdr:
                meta.update(hdr)
        body_text = normalize_pixiv_markers(body_text)
        body_text, up_copied, missing_ids = rewrite_uploaded_images(
            body_text, images_dir, image_search_dirs(src.parent, work)
        )
        copied.extend(up_copied)
        if suffix == ".md":
            body_text, md_copied, remote = collect_md_images(body_text, images_dir, src.parent)
            copied.extend(md_copied)
    if not meta.get("title"):
        meta["title"] = fname["title"]
    if not meta.get("author"):
        meta["author"] = None

    paras = split_paras(body_text)
    if len(paras) < 5:
        # ダウンロードTXTは行=段落(単一改行区切り)のことが多い。行単位で段落化して再分割
        lines = [ln.strip().lstrip("\u3000\t ") for ln in body_text.replace("\r\n", "\n").split("\n")]
        paras = [ln for ln in lines if ln]
    meta.update(guess_lang("\n".join(paras)))
    src_lang = meta.get("lang") or "zh"
    dst_lang = args.dst or {"zh": "ja", "ja": "zh"}.get(src_lang, "ja")
    if (src_lang, dst_lang) not in (("zh", "ja"), ("ja", "zh")):
        print(f"[ingest] 注意: 未対応の方向 {src_lang}→{dst_lang}(汎用 rule で処理)")
    meta["dst"] = dst_lang
    meta["n_paras"] = len(paras)
    meta["n_chars"] = sum(len(p) for p in paras)
    meta["images_copied"] = copied
    meta["remote_images"] = remote
    meta["images_missing"] = missing_ids
    meta["work_dir"] = str(work)

    atomic_write_text(work / "originals" / "source.md", "\n\n".join(paras) + "\n")
    save_structured(work / "manifest", meta)
    for sub in ("chapters", "analysis", "translation_input", "output", "output/_work", "logs"):
        (work / sub).mkdir(parents=True, exist_ok=True)

    print(f"[ingest] work: {work}")
    print(f"[ingest] title: {meta['title']} / author: {meta.get('author')}")
    print(f"[ingest] lang: {meta['lang']}→{meta['dst']} / script: {meta.get('han_script')} / paras: {meta['n_paras']} / chars: {meta['n_chars']}")
    if copied:
        print(f"[ingest] images: {len(copied)} copied")
    if remote:
        print(f"[ingest] remote image refs kept as-is: {len(remote)} (取得しない)")
    notice = missing_image_notice(missing_ids)
    if notice:
        print("[ingest] " + notice.replace("\n", "\n[ingest] "))


if __name__ == "__main__":
    main()
