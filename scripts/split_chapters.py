"""章分割・ブロック分割・段落採番。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    SCENE_BREAK, detect_duplicate_work, find_chapters, load_config, load_structured,
    make_blocks, save_chapters_meta, save_paras, split_paras, utf8,
)


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="章・ブロック分割")
    ap.add_argument("work", help="ワークスペースディレクトリ(manifestのある場所)")
    ap.add_argument("--target-chars", type=int, default=None, help="ブロック目標文字数(既定: config)")
    ap.add_argument("--numeric-headings", choices=("auto", "chapter", "section"), default="auto",
                    help="数字だけの行の扱い: auto=他に章見出しが無いときだけ章(既定) / chapter=常に章 / section=章にしない")
    ap.add_argument("--dedupe", action="store_true",
                    help="本文の後半が前半の繰り返しなら、後半を削除して分割する")
    ap.add_argument("--accept-duplicate", action="store_true",
                    help="重複を承知でそのまま分割する（manifest と chapters_meta に記録する）")
    args = ap.parse_args()
    with timed_stage(args.work, "split"):
        _stage_impl(args)


def _stage_impl(args: argparse.Namespace) -> None:

    work = Path(args.work)
    manifest = load_structured(work / "manifest")
    cfg = load_config()
    target = args.target_chars or int(cfg.get("split", {}).get("block_target_chars", 4000))

    src = work / "originals" / "source.md"
    paras = split_paras(src.read_text(encoding="utf-8"))
    lang = manifest.get("lang")
    numeric = args.numeric_headings

    found = find_chapters(paras, lang=lang, numeric=numeric)
    dup = detect_duplicate_work(paras, found)
    if dup["duplicate"] and not (args.dedupe or args.accept_duplicate):
        # 弱いモデルでも見落とさないよう、既定では何も書かずに停止する。
        # 続行には --dedupe か --accept-duplicate の明示が要る。
        print("[split] 警告: 本文が2回繰り返されています"
              f"（{dup['reason']}）")
        print(f"[split]   章数: {len(found)}（章題の並びが繰り返しになっています）")
        if dup["cut_chapter"] is not None:
            print(f"[split]   重複開始: 章 index {dup['cut_chapter']}（段落 {dup['cut_para']}）")
            print("[split]   対処: --dedupe で後半を削除して分割する")
        else:
            print("[split]   後半を自動で切る位置が決められません（章題が反復していない）")
            print("[split]   対処: 原文を確認し、必要なら手動で後半を削除してから再実行する")
        print("[split]   --accept-duplicate を付けると、重複を承知でこのまま分割します")
        stale = sorted((work / "chapters").glob("*.paras.toml")) if (work / "chapters").is_dir() else []
        if stale:
            print(f"[split] 注意: 既存の chapters/ に前回の分割結果 {len(stale)} 件が残っています"
                  "（この実行では更新していません）")
        print("[split] 何も書きませんでした。")
        sys.exit(1)

    if args.dedupe and dup["duplicate"]:
        print(f"[split] --dedupe: 重複（{dup['reason']}）を後半から削除します")
    elif args.accept_duplicate and dup["duplicate"]:
        print(f"[split] --accept-duplicate: 重複率 {dup['ratio']:.0%} を承知で続行します（記録します）")

    if args.dedupe and dup["duplicate"]:
        if dup["cut_para"] is None:
            print("[split] エラー: --dedupe の切断位置を決められません。"
                  "原文を確認して手動で後半を削除してください。", file=sys.stderr)
            sys.exit(1)
        removed = len(paras) - dup["cut_para"]
        paras = paras[:dup["cut_para"]]
        print(f"[split] --dedupe: 後半 {removed} 段落を削除しました")
        found = find_chapters(paras, lang=lang, numeric=numeric)
        again = detect_duplicate_work(paras, found)
        if again["duplicate"]:
            print(f"[split] 警告: 削除後も重複が残っています（{again['reason']}）。"
                  "原文を確認してください。")

    (work / "chapters").mkdir(parents=True, exist_ok=True)
    # 前回の分割が残した章ファイルを消す（章数が減ったときに古い章が残らないように）
    keep = {f"ch{i + 1:03d}.paras.toml" for i in range(len(found))}
    for old_f in (work / "chapters").glob("ch*.paras.toml"):
        if old_f.name not in keep:
            old_f.unlink()

    chapters = []
    ch_no = 0
    for title, s, e in found:
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
    work_meta["duplicate_check"] = {
        "ratio": round(dup["ratio"], 4),
        "deduped": bool(args.dedupe and dup["duplicate"]),
        "accepted": bool(args.accept_duplicate and dup["duplicate"] and not args.dedupe),
    }
    save_chapters_meta(work, {"work": work_meta, "chapters": chapters})
    total_blocks = sum(len(c["blocks"]) for c in chapters)
    print(f"[split] {len(chapters)} chapters / {total_blocks} blocks (target {target} chars)")


if __name__ == "__main__":
    main()
