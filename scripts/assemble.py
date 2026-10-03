"""確定済みブロックの結合: 章訳文・対訳・目次・全文。
対訳(章対訳・全文_対訳.md)は --parallel 指定で生成する。年齢正規化の記録
(analysis/age_policy_report-[ネタバレあり].md)がある作品では自動で有効。
全ブロック確定後は原文と中間物を消す(--keep-source で残す)。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    all_blocks_emitted, atomic_write_text, chapter_title, direction, load_chapters_meta, load_structured,
    load_titles, purge_source, save_chapters_meta, target_suffix, target_title, utf8,
)
from ingest import missing_image_notice, relink_markdown_files, retarget_md_images  # noqa: E402

AGE_REPORT = "age_policy_report-[ネタバレあり].md"


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="出力の結合")
    ap.add_argument("work")
    ap.add_argument("--keep-source", action="store_true", help="原文・中間物を残す(既定は全ブロック確定後に削除)")
    ap.add_argument("--parallel", action="store_true",
                    help="対訳(章対訳・全文_対訳.md)を生成する。年齢正規化の記録がある作品では指定不要(自動)")
    args = ap.parse_args()
    with timed_stage(args.work, "assemble"):
        _stage_impl(args)


def _stage_impl(args: argparse.Namespace) -> None:

    work = Path(args.work)
    _, dst = direction(work)
    suf = target_suffix(dst)
    meta = load_chapters_meta(work)
    titles = load_titles(work)
    tr_title = target_title(titles, dst)
    ch_titles = {c.get("id"): c for c in (titles.get("chapters") or []) if c.get("id")}
    out = work / "output"
    make_parallel = args.parallel or (work / "analysis" / AGE_REPORT).exists()
    toc = [f"# {tr_title}", "", "# 目次", ""] if tr_title else ["# 目次", ""]
    full_tr: list[str] = []
    full_par: list[str] = []
    done_any = False

    for c in meta["chapters"]:
        cid = c["id"]
        ch_tr: list[str] = []
        ch_par: list[str] = []
        blocks = c.get("blocks") or []
        n_done = 0
        heading = chapter_title(ch_titles.get(cid) or {}, dst, c.get("title") or cid)
        # 本文先頭行（章題段落）に同じテキストがある場合は自動見出しを付けない（二重見出し防止）
        first_line = ""
        for b_probe in blocks:
            f_probe = out / cid / f"{b_probe['id']}{suf}.md"
            if f_probe.exists():
                first_line = (f_probe.read_text(encoding="utf-8").strip().splitlines() or [""])[0].strip()
                break
        if heading and heading != tr_title and first_line.lstrip("# ").strip() != heading.strip():  # 全文冒頭と同文の見出しは省く
            ch_tr.append(f"# {heading}")
        for b in blocks:
            f_tr = out / cid / f"{b['id']}{suf}.md"
            if not f_tr.exists():
                continue
            ch_tr.append(f_tr.read_text(encoding="utf-8").strip())
            if make_parallel:
                f_par = out / cid / f"{b['id']}_parallel.md"
                if f_par.exists():
                    ch_par.append(f_par.read_text(encoding="utf-8").strip())
                else:
                    print(f"[assemble] WARN: 対訳がありません(スキップ): {cid}/{b['id']}")
            n_done += 1
            done_any = True
        if ch_tr:
            body = "\n\n".join(ch_tr) + "\n"
            atomic_write_text(out / f"{cid}{suf}.md", retarget_md_images(body))
            title = chapter_title(ch_titles.get(cid) or {}, dst, c.get("title") or cid)
            if make_parallel and ch_par:
                atomic_write_text(out / f"{cid}_parallel.md",
                                  retarget_md_images("\n\n".join(ch_par) + "\n"))
                toc.append(f"- [{title}]({cid}{suf}.md)（対訳: [{cid}_parallel.md]({cid}_parallel.md)）")
                full_par.append(f"<!-- {cid} -->\n" + "\n\n".join(ch_par))
            else:
                toc.append(f"- [{title}]({cid}{suf}.md)")
            full_tr.append(f"<!-- {cid} -->\n" + "\n\n".join(ch_tr))
        c["status"] = "done" if blocks and n_done == len(blocks) else c.get("status", "pending")

    if done_any:
        atomic_write_text(out / "目次.md", "\n".join(toc) + "\n")
        full_body = "\n\n".join(full_tr) + "\n"
        if tr_title:
            full_body = f"# {tr_title}\n\n" + full_body
        atomic_write_text(out / "全文_訳文.md", retarget_md_images(full_body))
        print(f"[assemble] 目次/全文を出力: {out / '目次.md'}")
        if make_parallel and full_par:
            atomic_write_text(out / "全文_対訳.md", retarget_md_images("\n\n".join(full_par) + "\n"))
            why = "--parallel" if args.parallel else f"年齢正規化の記録({AGE_REPORT})"
            print(f"[assemble] 対訳を出力: {out / '全文_対訳.md'} ({why})")
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
        removed = purge_source(work, keep_parallel=make_parallel)
        print(f"[assemble] 原文・中間物を削除 ({len(removed)}): " + ", ".join(removed[:12]) + ("…" if len(removed) > 12 else ""))
    elif args.keep_source:
        print("[assemble] --keep-source: 原文を残した")


if __name__ == "__main__":
    main()
