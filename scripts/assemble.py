"""確定済みブロックの結合: 章訳文・目次・全文。対訳は作らない。
全ブロック確定後は原文と中間物を消す(--keep-source で残す)。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    all_blocks_emitted, atomic_write_text, load_chapters_meta, load_structured,
    load_titles, purge_source, save_chapters_meta, utf8,
)
from ingest import missing_image_notice, relink_markdown_files, retarget_md_images  # noqa: E402


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="出力の結合")
    ap.add_argument("work")
    ap.add_argument("--keep-source", action="store_true", help="原文・中間物を残す(既定は全ブロック確定後に削除)")
    args = ap.parse_args()

    work = Path(args.work)
    meta = load_chapters_meta(work)
    titles = load_titles(work)
    title_ja = (titles.get("title_ja") or "").strip()
    ch_titles = {c.get("id"): c for c in (titles.get("chapters") or []) if c.get("id")}
    out = work / "output"
    toc = [f"# {title_ja}", "", "# 目次", ""] if title_ja else ["# 目次", ""]
    full_ja: list[str] = []
    done_any = False

    for c in meta["chapters"]:
        cid = c["id"]
        ch_ja: list[str] = []
        blocks = c.get("blocks") or []
        n_done = 0
        heading = (ch_titles.get(cid) or {}).get("ja") or c.get("title") or cid
        if heading and heading != title_ja:  # 全文冒頭と同文の見出しは省く
            ch_ja.append(f"# {heading}")
        for b in blocks:
            f_ja = out / cid / f"{b['id']}_ja.md"
            if not f_ja.exists():
                continue
            ch_ja.append(f_ja.read_text(encoding="utf-8").strip())
            n_done += 1
            done_any = True
        if ch_ja:
            ch_path = out / f"{cid}_ja.md"
            body = "\n\n".join(ch_ja) + "\n"
            atomic_write_text(ch_path, retarget_md_images(body))
            title = (ch_titles.get(cid) or {}).get("ja") or c.get("title") or cid
            toc.append(f"- [{title}]({cid}_ja.md)")
            full_ja.append(f"<!-- {cid} -->\n" + "\n\n".join(ch_ja))
        c["status"] = "done" if blocks and n_done == len(blocks) else c.get("status", "pending")

    if done_any:
        atomic_write_text(out / "目次.md", "\n".join(toc) + "\n")
        full_path = out / "全文_訳文.md"
        full_body = "\n\n".join(full_ja) + "\n"
        if title_ja:
            full_body = f"# {title_ja}\n\n" + full_body
        atomic_write_text(full_path, retarget_md_images(full_body))
        print(f"[assemble] 目次/全文を出力: {out / '目次.md'}")
    else:
        print("[assemble] 確定済みブロックなし")

    save_chapters_meta(work, meta)

    still_missing = relink_markdown_files(work)
    notice = missing_image_notice(still_missing)
    if not notice:
        try:
            man = load_structured(work / "manifest")
        except FileNotFoundError:
            man = {}
        leftover = [i for i in (man.get("images_missing") or []) if i]
        notice = missing_image_notice(leftover)
    if notice:
        print("[assemble] " + notice.replace("\n", "\n[assemble] "))

    if all_blocks_emitted(work, meta) and not args.keep_source:
        removed = purge_source(work)
        print(f"[assemble] 原文・中間物を削除 ({len(removed)}): " + ", ".join(removed[:12]) + ("…" if len(removed) > 12 else ""))
    elif args.keep_source:
        print("[assemble] --keep-source: 原文を残した")


if __name__ == "__main__":
    main()
