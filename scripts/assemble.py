"""確定済みブロックの結合: 章訳文・対訳・目次・全文。
対訳(章対訳・全文_対訳.md)は --parallel 指定で生成する。年齢正規化の記録
(analysis/age_policy_report-[ネタバレあり].md)がある作品では自動で有効。
全ブロック確定後は原文と中間物を消す(--keep-source で残す)。
titles.toml(analysis/)が無い・空のときは停止する。原題のまま出力するときだけ
--allow-src-title を指定する（未確定の章題は目次に「題未確定」と明示する）。"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    all_blocks_emitted, atomic_write_text, block_paras, chapter_title, direction, load_chapters_meta,
    load_paras, load_structured, load_titles, purge_source, save_chapters_meta, save_structured,
    target_suffix, target_title, utf8,
)
from ingest import (  # noqa: E402
    MD_IMAGE_SRC, missing_image_notice, relink_markdown_files, retarget_md_images,
)

AGE_REPORT = "age_policy_report-[ネタバレあり].md"


def _is_toml_table(v) -> bool:
    """dump_toml がテーブル([key]/[[key]])として書く値かどうか。"""
    return isinstance(v, dict) or (
        isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v))


def _record_assembled(work: Path, outputs: list[str]) -> None:
    """完成状態（assembled_at と出力成果物の一覧）を manifest に記録する（#20）。

    status / prepare / review はこの記録で「完成・中間物削除済み」を判定する。
    既存キー（skill_version 等）はそのまま残す。dump_toml はテーブル以降の
    スカラーを再読込時にテーブル内へ取り込むため、テーブル化される値は末尾へ
    回してから追記する（ingest と同じ注意）。
    """
    try:
        man = load_structured(work / "manifest")
    except (FileNotFoundError, OSError):
        man = {}
    scalars = {k: v for k, v in man.items() if not _is_toml_table(v)}
    tables = {k: v for k, v in man.items() if _is_toml_table(v)}
    scalars["assembled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    scalars["assembled_outputs"] = list(outputs)
    save_structured(work / "manifest", {**scalars, **tables})
    print(f"[assemble] 完成を記録: assembled_at={scalars['assembled_at']}"
          f"（出力成果物 {len(outputs)} 件）")


def _chapter_has_output(c: dict, out: Path, suf: str) -> bool:
    """この実行で章訳文として出力される章か（ブロック出力が1つ以上あるか）。"""
    return any((out / c["id"] / f"{b['id']}{suf}.md").exists() for b in (c.get("blocks") or []))


def _titles_gate_problems(work: Path, meta: dict, titles: dict, dst: str, out: Path, suf: str) -> list[str]:
    """titles ゲートの停止理由を返す（空リストなら通過）。

    原題が黙って出力に載る経路だけを挙げる:
      - titles.toml が無い/空（load_titles が {} を返す状態）
      - 出力対象の章に原文見出しがあり、titles.toml に訳題が無い
    """
    if not titles:
        if (work / "analysis" / "titles.toml").exists():
            return ["analysis/titles.toml が空です（分析一周後に章題を確定して書き込んでください）"]
        return ["analysis/titles.toml がありません（分析一周後に章題を確定して作成してください）"]
    other = "ja" if dst == "zh" else "zh"
    ch_titles = {c.get("id"): c for c in (titles.get("chapters") or []) if c.get("id")}
    undetermined: list[str] = []
    for c in meta["chapters"]:
        if not (c.get("title") or "").strip():
            continue  # 原文見出しを持たない章は記号見出しで出るため対象外
        if not _chapter_has_output(c, out, suf):
            continue  # この実行で出力しない章は対象外（部分 assemble を妨げない）
        entry = ch_titles.get(c["id"]) or {}
        if not (entry.get(dst) or entry.get(other)):
            undetermined.append(c["id"])
    if undetermined:
        return ["章題が未確定です: " + ", ".join(undetermined)
                + "（titles.toml の [[chapters]] に訳題を追加してください）"]
    return []


def _image_ref_names(text: str) -> set[str]:
    """Markdown の画像参照（images/ 配下）からファイル名を集める。"""
    names: set[str] = set()
    for m in MD_IMAGE_SRC.finditer(text):
        name = m.group(2).strip()
        if name:
            names.add(name)
    return names


def _image_gate_problems(work: Path, meta: dict, out: Path, suf: str) -> list[str]:
    """画像参照ゲートの停止理由を返す（空リストなら通過）。S4 / P0-1。

    確定出力（output/<ch>/<block><suf>.md）ごとに、
      - 参照先ファイルが output/images/ に実在すること
      - 画像参照が原文（chapters/<ch>.paras.toml の該当段落）と一致すること
    を検査する。原文が読めない章（purge 後など）は実在検査のみ行う。
    """
    problems: list[str] = []
    for c in meta["chapters"]:
        cid = c["id"]
        try:
            src_paras = load_paras(work, cid)
        except FileNotFoundError:
            src_paras = None
        for b in c.get("blocks") or []:
            f = out / cid / f"{b['id']}{suf}.md"
            if not f.exists():
                continue
            out_refs = _image_ref_names(f.read_text(encoding="utf-8"))
            for name in sorted(out_refs):
                if not (out / "images" / name).is_file():
                    problems.append(
                        f"{cid}/{b['id']}: 参照先の画像が output/images/ にありません: images/{name}")
            if src_paras is None:
                continue
            src_refs: set[str] = set()
            for p in block_paras(src_paras, b):
                src_refs |= _image_ref_names(p.get("text") or "")
            if out_refs != src_refs:
                detail: list[str] = []
                extra = sorted(out_refs - src_refs)
                lost = sorted(src_refs - out_refs)
                if extra:
                    detail.append("訳文のみ: " + ", ".join(f"images/{x}" for x in extra))
                if lost:
                    detail.append("原文のみ: " + ", ".join(f"images/{x}" for x in lost))
                problems.append(
                    f"{cid}/{b['id']}: 訳文の画像参照が原文と一致しません（{' / '.join(detail)}）")
    return problems


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="出力の結合")
    ap.add_argument("work")
    ap.add_argument("--keep-source", action="store_true", help="原文・中間物を残す(既定は全ブロック確定後に削除)")
    ap.add_argument("--parallel", action="store_true",
                    help="対訳(章対訳・全文_対訳.md)を生成する。年齢正規化の記録がある作品では指定不要(自動)")
    ap.add_argument("--allow-src-title", action="store_true",
                    help="titles.toml が無い/題未確定のとき原題のまま出力する（目次に「題未確定」を明示）")
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
    allow_src_title = args.allow_src_title
    # titles ゲート: 訳題が未確定のまま原題を黙って出さない（レビュー #2 / P0-2）。
    # 通常経路はここで停止し、--allow-src-title の経路だけ原題のまま続行する。
    problems = _titles_gate_problems(work, meta, titles, dst, out, suf)
    if problems and not allow_src_title:
        for p in problems:
            print(f"[assemble] {p}")
        print("[assemble] 原題のまま出力する場合は --allow-src-title を指定してください。")
        sys.exit(1)
    if allow_src_title:
        print("[assemble] --allow-src-title: 原題のまま出力する経路で実行します"
              "（未確定の章題は目次に「題未確定」を明示）")
    # 画像参照ゲート: 確定出力の参照が原文と一致し、参照先が output/images/ に実在すること。
    # 欠落・不一致は理由を出して停止する（回避フラグは作らない。S4 / P0-1）。
    img_problems = _image_gate_problems(work, meta, out, suf)
    if img_problems:
        print("[assemble] 画像参照の検査で停止しました:")
        for ip in img_problems:
            print(f"[assemble]   - {ip}")
        print("[assemble] 参照先の画像を output/images/ に置くか、"
              "訳文の参照を原文に合わせてから再実行してください。")
        sys.exit(1)
    make_parallel = args.parallel or (work / "analysis" / AGE_REPORT).exists()
    toc = [f"# {tr_title}", "", "# 目次", ""] if tr_title else ["# 目次", ""]
    full_tr: list[str] = []
    full_par: list[str] = []
    done_any = False
    outputs: list[str] = []  # 完成記録に残す出力成果物（work 相対）

    for c in meta["chapters"]:
        cid = c["id"]
        ch_tr: list[str] = []
        ch_par: list[str] = []
        blocks = c.get("blocks") or []
        n_done = 0
        # 章題の解決: 訳題が無い章は未確定。原題のまま出すのは --allow-src-title の経路だけ
        # （通常経路の未確定はゲートで停止済みで、残るのは原題を持たない章のみ）。
        entry = ch_titles.get(cid) or {}
        src_title = (c.get("title") or "").strip()
        resolved = chapter_title(entry, dst)
        undetermined = resolved is None
        if resolved is not None:
            title = resolved
        elif allow_src_title:
            title = src_title or cid
        else:
            title = cid  # 原題を持たない章の記号見出し
        # 本文先頭行（章題段落）に同じテキストがある場合は自動見出しを付けない（二重見出し防止）
        first_line = ""
        for b_probe in blocks:
            f_probe = out / cid / f"{b_probe['id']}{suf}.md"
            if f_probe.exists():
                first_line = (f_probe.read_text(encoding="utf-8").strip().splitlines() or [""])[0].strip()
                break
        if title and title != tr_title and first_line.lstrip("# ").strip() != title.strip():  # 全文冒頭と同文の見出しは省く
            ch_tr.append(f"# {title}")
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
            outputs.append(f"output/{cid}{suf}.md")
            line = f"- [{title}]({cid}{suf}.md)"
            if make_parallel and ch_par:
                atomic_write_text(out / f"{cid}_parallel.md",
                                  retarget_md_images("\n\n".join(ch_par) + "\n"))
                outputs.append(f"output/{cid}_parallel.md")
                line += f"（対訳: [{cid}_parallel.md]({cid}_parallel.md)）"
                full_par.append(f"<!-- {cid} -->\n" + "\n\n".join(ch_par))
            if undetermined and allow_src_title:
                line += "（題未確定）"  # 原題のまま出した章題を目次で明示（回避の記録）
            toc.append(line)
            full_tr.append(f"<!-- {cid} -->\n" + "\n\n".join(ch_tr))
        c["status"] = "done" if blocks and n_done == len(blocks) else c.get("status", "pending")

    if done_any:
        atomic_write_text(out / "目次.md", "\n".join(toc) + "\n")
        outputs.append("output/目次.md")
        full_body = "\n\n".join(full_tr) + "\n"
        if tr_title:
            full_body = f"# {tr_title}\n\n" + full_body
        atomic_write_text(out / "全文_訳文.md", retarget_md_images(full_body))
        outputs.append("output/全文_訳文.md")
        print(f"[assemble] 目次/全文を出力: {out / '目次.md'}")
        if make_parallel and full_par:
            atomic_write_text(out / "全文_対訳.md", retarget_md_images("\n\n".join(full_par) + "\n"))
            outputs.append("output/全文_対訳.md")
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
        # 全ブロック確定の assemble が通った = 「完成・中間物削除済み」。
        # status / prepare / review の誤認防止のため manifest に記録する（#20）。
        _record_assembled(work, outputs)
    elif args.keep_source:
        print("[assemble] --keep-source: 原文を残した")


if __name__ == "__main__":
    main()
