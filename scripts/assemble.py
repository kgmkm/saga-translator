"""確定済みブロックの結合: 章訳文・対訳・目次・全文。
対訳(章対訳・全文_対訳.md)は既定で生成する(--no-parallel で抑制)。全文(全文_訳文.md)
も章数によらず既定で生成する(--no-full で抑制。単章作品で章ファイルとの重複を避けたい
ときだけ抑制する)。全ブロック確定後は原文と中間物を消す(--keep-source で残す)が、
chapters/*.paras.toml(改訂路 --revise の原文)と単章作品の output/<ch>/(ブロック訳)は
残し、監査記録(output/_work/commit・output/_work/done)と改名の記録
(_work/renames.jsonl)も残して、assemble の最後に「残る中間物」として一覧表示する。
空の組立て(確定済みブロックなし)はエラー終了(exit 1)。purge の前に成果物の実在・
サイズを確認し、不備があれば原文を消さずに停止する。
titles.toml(analysis/)が無い・空のときは停止する。原題のまま出力するときだけ
--allow-src-title を指定する（未確定の章題は目次に「題未確定」と明示する）。"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    all_blocks_emitted, atomic_write_text, block_paras, chapter_title, direction, load_chapters_meta,
    load_config,
    load_paras, load_structured, load_titles, purge_source, save_chapters_meta, save_structured,
    target_suffix, target_title, utf8,
)
from ingest import (  # noqa: E402
    MD_IMAGE_SRC, missing_image_notice, relink_markdown_files, retarget_md_images,
)


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
    成果物はバイト数を併記する（AS-3: 「N 件」だけでは空出力・縮退に気づけない）。
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
    _print_output_sizes(work, outputs)


def _print_output_sizes(work: Path, outputs: list[str]) -> None:
    """成果物の一覧をバイト数つきで出す。空(0B)の成果物は警告する（AS-3）。"""
    sizes: list[tuple[str, int | None]] = []
    for rel in outputs:
        try:
            sizes.append((rel, (work / rel).stat().st_size))
        except OSError:
            sizes.append((rel, None))
    shown = ", ".join(
        f"{rel}（不明）" if size is None else f"{rel} {size:,}B" for rel, size in sizes[:12])
    if len(sizes) > 12:
        shown += f"…（他 {len(sizes) - 12} 件）"
    total = sum(size for _rel, size in sizes if size is not None)
    print(f"[assemble] 出力サイズ: {shown} ／ 合計 {total:,}B")
    empty = [rel for rel, size in sizes if size == 0]
    if empty:
        print("[assemble] WARN: 空(0B)の成果物があります: " + ", ".join(empty))


def _artifact_problems(work: Path, outputs: list[str]) -> list[str]:
    """purge の前に成果物（この実行で書いた出力）の実在とサイズを確認する（G23/AS-1）。

    不備（欠落・空ファイル）があれば理由の一覧を返す。呼び出し側はこれが
    空でないとき purge を実行しない（原文を消して復元不能になるのを防ぐ）。
    """
    problems: list[str] = []
    for rel in outputs:
        p = work / rel
        if not p.is_file():
            problems.append(f"{rel}: 成果物がありません")
            continue
        try:
            if p.stat().st_size == 0:
                problems.append(f"{rel}: 成果物が空です(0B)")
        except OSError as e:  # pragma: no cover - 環境依存
            problems.append(f"{rel}: サイズを確認できません ({e})")
    return problems


def _remaining_intermediates(work: Path) -> list[tuple[str, str]]:
    """完成後も残る中間物を (work 相対パス, 種類) の一覧で返す（レビュー P3）。

    監査保持契約（#20・P3-2）で purge せず残すものを列挙する:
      - output/_work/commit/ : 監査記録（commit の報告・段落対応スナップショット）
      - output/_work/done/   : 退避済み done（確定ドラフト）
      - _work/renames.jsonl  : 改名の記録（rename_terms の監査記録）
    """
    items: list[tuple[str, str]] = []
    commit = work / "output" / "_work" / "commit"
    if commit.is_dir():
        for p in sorted(commit.iterdir()):
            items.append((f"output/_work/commit/{p.name}", "監査記録"))
    done = work / "output" / "_work" / "done"
    if done.is_dir():
        for p in sorted(done.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(done)).replace("\\", "/")
                items.append((f"output/_work/done/{rel}", "退避済み done"))
    if (work / "_work" / "renames.jsonl").is_file():
        items.append(("_work/renames.jsonl", "改名の記録"))
    return items


def _print_remaining_intermediates(work: Path) -> None:
    """完成後も残る中間物を `[assemble] 残る中間物: …` の1行で列挙する（レビュー P3）。"""
    items = _remaining_intermediates(work)
    if not items:
        print("[assemble] 残る中間物: なし")
        return
    shown = ", ".join(f"{rel}（{kind}）" for rel, kind in items[:12])
    if len(items) > 12:
        shown += f"…（他 {len(items) - 12} 件）"
    print(f"[assemble] 残る中間物: {shown}")


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


def _fidelity_problems(work: Path, meta: dict, out: Path, suf: str) -> list[str]:
    """確定済みブロックの訳文を原文と突き合わせ、未翻訳コピーを検出する。

    ブロック単位の commit で通ったものだけを見る（未確定は対象外）。
    段落数不一致は注意（訳文の編集で起きうる）にとどめ、コピー疑いだけを返す。
    """
    from fidelity import cfg_from, check_block, check_pair, describe
    cfg = cfg_from(load_config())
    problems: list[str] = []
    for c in meta["chapters"]:
        cid = c["id"]
        try:
            src_map = {int(p["n"]): str(p.get("text") or "")
                       for p in (load_paras(work, cid).get("paras") or [])}
        except (FileNotFoundError, ValueError):
            continue  # 原文が無い章（purge 後など）は対象外
        for b in c.get("blocks") or []:
            f = out / cid / f"{b['id']}{suf}.md"
            if not f.is_file():
                continue
            text = f.read_text(encoding="utf-8")
            paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            s0, s1 = b["paras"]
            expected = s1 - s0 + 1
            pairs: list[tuple[str, str]] = []
            if len(paras) != expected:
                # マーカー落とし後の再分割は原文の段落構成と一致しないことがある
                # （原文段落内の改行・空行）。段落対応が崩れるので個別判定はせず、
                # ブロック全体の判定だけを行う。
                print(f"[assemble] 注意: {cid}/{b['id']} の段落数が原文と一致しません"
                      f"（訳文 {len(paras)} / 原文 {expected}）。段落単位の検査は省略します")
                # ブロック全体の判定には原文の連結を使う（対応は取らない）。
                pairs = [(src_map.get(s0 + i, ""), t) for i, t in enumerate(paras)]
            else:
                for i, t in enumerate(paras):
                    st = src_map.get(s0 + i, "")
                    pairs.append((st, t))
                    r = check_pair(st, t, cfg)
                    if r["flag"]:
                        problems.append(f"{cid}/{b['id']} P{s0 + i}: {describe(r)}")
            blk = check_block(pairs, cfg)
            if blk["flag"]:
                problems.append(f"{cid}/{b['id']}: ブロックのカナ比率 {blk['kana']:.2f}"
                                "（原文を写した疑い）")
    return problems


def _record_quality_waiver(work: Path, reason: str, problems: list[str]) -> None:
    """--allow-quality の記録を manifest に残す（後から理由を追えるように）。"""
    try:
        man = load_structured(work / "manifest")
    except FileNotFoundError:
        man = {}
    items = man.get("quality_waived") or []
    items.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "reason": reason, "count": len(problems), "examples": problems[:5]})
    man["quality_waived"] = items
    save_structured(work / "manifest", man)


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
                    help="対訳を生成する（既定で有効。互換のため受理。抑制は --no-parallel）")
    ap.add_argument("--no-parallel", action="store_true",
                    help="対訳(章対訳・全文_対訳.md)を生成しない")
    ap.add_argument("--allow-quality", metavar="REASON", default=None,
                    help="忠実性検査の指摘を承知で続行する（理由を manifest に記録）")
    ap.add_argument("--allow-src-title", action="store_true",
                    help="titles.toml が無い/題未確定のとき原題のまま出力する（目次に「題未確定」を明示）")
    ap.add_argument("--keep-full", action="store_true",
                    help="全文(全文_訳文.md・全文_対訳.md)を出力する（既定で有効。互換のため受理）")
    ap.add_argument("--no-full", action="store_true",
                    help="全文(全文_訳文.md・全文_対訳.md)を出力しない（章ファイルのみ）")
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
    # 全文・対訳は既定で出力する（G25 / G26）。抑制は --no-full / --no-parallel。
    # --keep-full / --parallel は互換のため受理する（指定しても結果は既定と同じ）。
    emit_full = not args.no_full
    make_parallel = not args.no_parallel
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
    # 忠実性ゲート: 原文を写しただけの訳文を完成物に混ぜない。
    # 既定は停止。--allow-quality "理由" のときだけ記録して続行する。
    fid_problems = _fidelity_problems(work, meta, out, suf)
    if fid_problems:
        if args.allow_quality:
            print(f"[assemble] --allow-quality: 忠実性の指摘 {len(fid_problems)} 件を承知で続行します")
            for fp in fid_problems[:8]:
                print(f"[assemble]   - {fp}")
            _record_quality_waiver(work, args.allow_quality, fid_problems)
        else:
            print("[assemble] 忠実性検査で停止しました（未翻訳コピーの疑い）:")
            for fp in fid_problems[:12]:
                print(f"[assemble]   - {fp}")
            if len(fid_problems) > 12:
                print(f"[assemble]   …他 {len(fid_problems) - 12} 件")
            print("[assemble] 対処: 該当ブロックを commit --revise で訳し直してください。"
                  "原文が中国語のまま残る正当な段落なら"
                  " --allow-quality \"理由\" で記録して続行できます。")
            sys.exit(1)
    toc = [f"# {tr_title}", "", "# 目次", ""] if tr_title else ["# 目次", ""]
    full_tr: list[str] = []
    full_par: list[str] = []
    done_any = False
    missing_blocks: list[str] = []  # この実行で出力が無かったブロック（部分組立ての警告用）
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
                missing_blocks.append(f"{cid}/{b['id']}")
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
        if ch_tr and n_done:
            # ブロック出力が1つも無い章はファイルを書かない（見出しだけの縮退で
            # 既存の章ファイルを壊さない。G23/AS-1 の 9B 破壊の再発防止）。
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
        if missing_blocks:
            # 部分組立ては妨げない（復旧・部分確認のため）が、完成扱いにはせず警告する。
            shown_missing = ", ".join(missing_blocks[:8]) + ("…" if len(missing_blocks) > 8 else "")
            print("[assemble] WARN: 未確定のブロックがあります"
                  f"（部分組立て・{len(missing_blocks)}件）: {shown_missing}")
            print("[assemble] WARN: 確定済みブロックのみで組み立てました（purge・完成記録はしません）")
        atomic_write_text(out / "目次.md", "\n".join(toc) + "\n")
        outputs.append("output/目次.md")
        print(f"[assemble] 目次を出力: {out / '目次.md'}")
        if emit_full:
            full_body = "\n\n".join(full_tr) + "\n"
            if tr_title:
                full_body = f"# {tr_title}\n\n" + full_body
            atomic_write_text(out / "全文_訳文.md", retarget_md_images(full_body))
            outputs.append("output/全文_訳文.md")
            print(f"[assemble] 全文を出力: {out / '全文_訳文.md'}")
        else:
            print("[assemble] --no-full: 全文_訳文.md は出力しません")
        if make_parallel and full_par:
            if emit_full:
                atomic_write_text(out / "全文_対訳.md", retarget_md_images("\n\n".join(full_par) + "\n"))
                outputs.append("output/全文_対訳.md")
                print(f"[assemble] 対訳を出力: {out / '全文_対訳.md'}")
            else:
                print("[assemble] --no-full: 全文_対訳.md は出力しません")
    else:
        # 空の組立て: 何も書かず・何も消さずにエラー終了する（G23/AS-1）。
        print("[assemble] 確定済みブロックなし: 組み立てる対象がありません")
        print("[assemble] 対処: 各ブロックを saga.py commit で確定してから再実行してください。"
              "原文は chapters/*.paras.toml に残っており、確定済みドラフトは"
              " output/_work/done/ から再取得できます。")
        sys.exit(1)

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

    complete = all_blocks_emitted(work, meta)
    if complete and not args.keep_source:
        # purge の前に成果物の実在・サイズを確認する（G23/AS-1）。不備があれば
        # 原文を消さずに停止する（purge 後に空出力へ気づいても原文は戻せない）。
        problems = _artifact_problems(work, outputs)
        if problems:
            print("[assemble] 成果物の確認で停止しました（purge は行いません）:")
            for p in problems:
                print(f"[assemble]   - {p}")
            print("[assemble] 出力を確認して再実行してください（原文・中間物は残っています）。")
            sys.exit(1)
        removed = purge_source(work, keep_parallel=make_parallel)
        print(f"[assemble] 原文・中間物を削除 ({len(removed)}): " + ", ".join(removed[:12]) + ("…" if len(removed) > 12 else ""))
        # 全ブロック確定の assemble が通った = 「完成・中間物削除済み」。
        # status / prepare / review の誤認防止のため manifest に記録する（#20）。
        _record_assembled(work, outputs)
    elif args.keep_source:
        print("[assemble] --keep-source: 原文を残した")
    if complete:
        # 完成後も残る中間物（監査記録・退避済み done・renames.jsonl）の一覧（レビュー P3）。
        _print_remaining_intermediates(work)


if __name__ == "__main__":
    main()
