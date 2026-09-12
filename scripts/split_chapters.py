"""章分割・ブロック分割・段落採番。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    SCENE_BREAK, find_chapters, load_config, load_structured,
    make_blocks, save_chapters_meta, save_paras, split_paras, utf8,
)


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="章・ブロック分割")
    ap.add_argument("work", help="ワークスペースディレクトリ(manifestのある場所)")
    ap.add_argument("--target-chars", type=int, default=None, help="ブロック目標文字数(既定: config)")
    args = ap.parse_args()

    work = Path(args.work)
    manifest = load_structured(work / "manifest")
    cfg = load_config()
    target = args.target_chars or int(cfg.get("split", {}).get("block_target_chars", 4000))

    src = work / "originals" / "source.md"
    paras = split_paras(src.read_text(encoding="utf-8"))
    (work / "chapters").mkdir(parents=True, exist_ok=True)
    lang = manifest.get("lang")

    chapters = []
    ch_no = 0
    for title, s, e in find_chapters(paras, lang=lang):
        ch_no += 1
        cid = f"ch{ch_no:03d}"
        cps = paras[s:e]
        blocks = make_blocks(cps, target)
        para_list = [{"n": i + 1, "text": t} for i, t in enumerate(cps)]
        save_paras(work, cid, {"chapter": cid, "title": title or "", "paras": para_list})
        chapters.append({
            "id": cid,
            "title": title or "",
            "n_paras": len(cps),
            "n_chars": sum(len(p) for p in cps),
            "blocks": [
                {
                    "id": f"b{i + 1}",
                    "paras": [s2 + 1, e2],
                    "n_chars": sum(len(cps[k]) for k in range(s2, e2)),
                    "scene_break_cut": SCENE_BREAK.match(cps[e2 - 1]) is not None if e2 > s2 else False,
                }
                for i, (s2, e2) in enumerate(blocks)
            ],
            "status": "pending",
        })
        print(f"[split] {cid}: title={title!r} paras={len(cps)} chars={chapters[-1]['n_chars']} blocks={len(blocks)}")

    work_meta = {k: manifest.get(k) or "" for k in ("work_id", "part", "title", "author", "lang")}
    save_chapters_meta(work, {"work": work_meta, "chapters": chapters})
    total_blocks = sum(len(c["blocks"]) for c in chapters)
    print(f"[split] {len(chapters)} chapters / {total_blocks} blocks (target {target} chars)")


if __name__ == "__main__":
    main()
