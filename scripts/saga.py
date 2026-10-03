"""saga-translator CLI エントリポイント。ステータス確認と分析コミット。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    atomic_write_text, direction, dump_toml, load_chapters_meta,
    load_characters, load_plot, load_terms, load_toml,
    save_chapters_meta, save_toml, target_suffix, utf8,
)

PLOT_RE = re.compile(r'^block = "([^"]+)"', re.M)

# ---------------------------------------------------------------------------
# status サブコマンド
# ---------------------------------------------------------------------------

def _count_blocks(chapters: list[dict]) -> int:
    return sum(len(ch.get("blocks", [])) for ch in chapters)


def _status_analysis(work: Path, chapters: list[dict]) -> tuple[int, int, list[str]]:
    """analysis 進捗。戻り値: (done, total, warnings)"""
    total = _count_blocks(chapters)
    done = 0
    warnings: list[str] = []
    # plot.toml から実体ブロックを収集
    p_plot = work / "analysis" / "plot-[ネタバレあり].toml"
    p_plot_json = work / "analysis" / "plot-[ネタバレあり].json"
    plot_blocks: set[str] = set()
    if p_plot.is_file():
        plot_blocks = set(PLOT_RE.findall(p_plot.read_text(encoding="utf-8")))
    elif p_plot_json.is_file():
        try:
            data = load_toml(p_plot_json)
            for e in data.get("entries", []):
                b = str(e.get("block", ""))
                if b:
                    plot_blocks.add(b)
        except Exception:
            pass

    for ch in chapters:
        cid = ch["id"]
        for b in ch.get("blocks", []):
            bid = b["id"]
            key = f"{cid}/{bid}"
            a_flag = bool(b.get("analysis_started_at") or b.get("analysis_committed_at") or b.get("analysis_done_at"))
            ana = key in plot_blocks
            if ana:
                done += 1
            if a_flag and not ana:
                warnings.append(f"  {key}: フラグのみ（実体なし）")
            if ana and not a_flag:
                warnings.append(f"  {key}: 実体のみ（フラグなし）")
    return done, total, warnings


def _status_translation(work: Path, chapters: list[dict]) -> tuple[int, int, list[str]]:
    """翻訳進捗。戻り値: (done, total, warnings)"""
    total = _count_blocks(chapters)
    done = 0
    _, dst = direction(work)
    suffix = target_suffix(dst)
    warnings: list[str] = []

    for ch in chapters:
        cid = ch["id"]
        out_dir = work / "output" / cid
        for b in ch.get("blocks", []):
            bid = b["id"]
            key = f"{cid}/{bid}"
            tr = (out_dir / f"{bid}{suffix}.md").is_file()
            t_flag = bool(b.get("translation_done_at"))
            if tr:
                done += 1
            if t_flag and not tr:
                warnings.append(f"  {key}: tr-フラグのみ（実体なし）")
            if tr and not t_flag:
                # 翻訳ファイルが存在するのに翻訳完了フラグがない——emit 由来で普通にある
                pass
    return done, total, warnings


def _next_work(chapters: list[dict]) -> str | None:
    """次の未翻訳ブロック（最後の翻訳ブロックの次、または context 作成済みの未翻訳）。"""
    _, dst = direction(Path.cwd())  # placeholder — caller supplies work
    suffix = target_suffix(dst)
    for ch in chapters:
        cid = ch["id"]
        for b in ch.get("blocks", []):
            bid = b["id"]
            # この関数は work を知らないので、ここではキーだけ返す
            a = bool(b.get("analysis_started_at") or b.get("analysis_committed_at") or b.get("analysis_done_at"))
            t = bool(b.get("translation_done_at"))
            if a and not t:
                return f"{cid}/{bid}"
    return None


def cmd_status(work: Path) -> int:
    if not work.is_dir():
        print(f"[status] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1
    meta_p = work / "chapters_meta.toml"
    if not meta_p.is_file():
        meta_p = work / "chapters_meta.json"
    if not meta_p.is_file():
        print(f"[status] エラー: chapters_meta がありません: {work}", file=sys.stderr)
        return 1

    meta = load_chapters_meta(work)
    chapters = meta.get("chapters", [])
    if not chapters:
        print(f"[status] エラー: chapters が空です", file=sys.stderr)
        return 1

    a_done, a_total, a_warns = _status_analysis(work, chapters)
    t_done, t_total, t_warns = _status_translation(work, chapters)
    all_warns = a_warns + t_warns

    print(f"[status] work={work.name} 進捗: 分析 {a_done}/{a_total} ブロック、翻訳 {t_done}/{t_total} ブロック")

    # 次の作業
    next_key = _next_work(chapters)
    # 実体ベースで next 再判定 — 直接 work が分かるように
    _, dst = direction(work)
    suffix = target_suffix(dst)
    next_key = None
    for ch in chapters:
        cid = ch["id"]
        out_dir = work / "output" / cid
        for b in ch.get("blocks", []):
            bid = b["id"]
            key = f"{cid}/{bid}"
            a = bool(b.get("analysis_started_at") or b.get("analysis_committed_at") or b.get("analysis_done_at"))
            t_flag = bool(b.get("translation_done_at"))
            tr = (out_dir / f"{bid}{suffix}.md").is_file()
            if a and not (t_flag or tr):
                next_key = key
                break
        if next_key:
            break

    if next_key:
        print(f"[status] 次の作業: {next_key} の翻訳（context 作成済み）")
    elif a_done < a_total:
        print(f"[status] 次の作業: 分析（未分析ブロックあり）")
    else:
        print(f"[status] 次の作業: なし（全ブロック完了）")

    if all_warns:
        print("[status] 警告:")
        for w in all_warns:
            print(f"[status]   {w}")
    else:
        print("[status] 警告: なし")

    return 0


# ---------------------------------------------------------------------------
# analyze-commit サブコマンド
# ---------------------------------------------------------------------------

def _parse_delta(path: Path) -> dict:
    """差分TOMLを読み込む。"""
    raw = path.read_text(encoding="utf-8")
    return load_toml(path)


def _find_entry_by_key(entries: list[dict], key: str, value: str) -> int | None:
    for i, e in enumerate(entries):
        if str(e.get(key, "")).strip() == value.strip():
            return i
    return None


def _apply_chars_delta(chars: list[dict], delta_list: list[dict]) -> list[str]:
    """キャラ差分を適用。proposed が発生した変更を説明する文字列リストを返す。"""
    proposed_log: list[str] = []
    SPECIAL = {"match_key", "match_value", "updates", "new_fields",
               "characters_delta", "terms_delta", "plot_delta", "block"}
    protect_keys = {"name_ja", "role", "first_person", "second_person",
                    "speech_style", "vocab", "catchphrase", "ruby"}
    for delta in delta_list:
        match_key = delta.get("match_key", "name_src")
        match_value = delta.get("match_value", "")
        idx = _find_entry_by_key(chars, match_key, match_value)
        if idx is not None:
            entry = chars[idx]
            # すべての変更を集める（updates/new_fields/フラットキー）
            updates: dict[str, object] = {}
            updates.update(delta.get("updates") or {})
            updates.update(delta.get("new_fields") or {})
            for k, v in delta.items():
                if k not in SPECIAL:
                    updates[k] = v
            for k, v in updates.items():
                existing = entry.get(k)
                if existing is not None and str(existing).strip() and k in protect_keys:
                    proposed = entry.get("proposed") or ""
                    line = f"{k}: 「{existing}」→「{v}」"
                    if line not in proposed:
                        proposed = (proposed + "\n" + line).strip()
                    entry["proposed"] = proposed
                    proposed_log.append(f"{match_value}/{k}: →proposed")
                else:
                    entry[k] = v
        else:
            # 新規エントリ
            new_entry = dict(delta)
            new_entry.pop("match_key", None)
            new_entry.pop("match_value", None)
            new_entry.pop("updates", None)
            new_entry.pop("new_fields", None)
            base = {match_key: match_value}
            for k, v in (delta.get("updates") or {}).items():
                base[k] = v
            for k, v in (delta.get("new_fields") or {}).items():
                base[k] = v
            for k, v in delta.items():
                if k not in SPECIAL:
                    base[k] = v
            chars.append(base)
    return proposed_log


def _apply_terms_delta(terms: list[dict], delta_list: list[dict]) -> list[str]:
    """用語差分を適用。proposed ログを返す。"""
    proposed_log: list[str] = []
    SPECIAL = {"match_key", "match_value", "updates", "new_fields",
               "characters_delta", "terms_delta", "plot_delta", "block"}
    protect_keys = {"term_ja", "ruby", "memo"}
    for delta in delta_list:
        match_key = delta.get("match_key", "term_src")
        match_value = delta.get("match_value", "")
        idx = _find_entry_by_key(terms, match_key, match_value)
        if idx is not None:
            entry = terms[idx]
            updates: dict[str, object] = {}
            updates.update(delta.get("updates") or {})
            updates.update(delta.get("new_fields") or {})
            for k, v in delta.items():
                if k not in SPECIAL:
                    updates[k] = v
            for k, v in updates.items():
                existing = entry.get(k)
                if existing is not None and str(existing).strip() and k in protect_keys:
                    proposed = entry.get("proposed") or ""
                    line = f"{k}: 「{existing}」→「{v}」"
                    if line not in proposed:
                        proposed = (proposed + "\n" + line).strip()
                    entry["proposed"] = proposed
                    proposed_log.append(f"{match_value}/{k}: →proposed")
                else:
                    entry[k] = v
        else:
            base = {match_key: match_value}
            for k, v in (delta.get("updates") or {}).items():
                base[k] = v
            for k, v in (delta.get("new_fields") or {}).items():
                base[k] = v
            for k, v in delta.items():
                if k not in SPECIAL:
                    base[k] = v
            terms.append(base)
    return proposed_log


def _apply_plot_delta(entries: list[dict], delta_list: list[dict]) -> list[str]:
    """プロットエントリを追記。冪等性: 同一 block + 同一 text は重複スキップ。"""
    proposed_log: list[str] = []
    for delta in delta_list:
        block = delta.get("block", "")
        text = (delta.get("updates") or {}).get("text") or delta.get("text", "")
        # 冪等性チェック: 既存に同じ block+text があればスキップ
        if block and text:
            dup = any(
                str(e.get("block", "")).strip() == block.strip()
                and str(e.get("text", "")).strip() == text.strip()
                for e in entries
            )
            if dup:
                continue
        new_entry = {}
        if block:
            new_entry["block"] = block
        if text:
            new_entry["text"] = text
        for k, v in (delta.get("updates") or {}).items():
            if k not in ("block", "text"):
                new_entry[k] = v
        for k, v in (delta.get("new_fields") or {}).items():
            new_entry[k] = v
        entries.append(new_entry)
    return proposed_log


def _merge_delta(work: Path, delta: dict) -> tuple[bool, list[str]]:
    """差分を3ファイルにマージ。(成功, ログ)"""
    chars = load_characters(work)
    terms = load_terms(work)
    plot = load_plot(work)

    chars_list = chars.get("characters", [])
    terms_list = terms.get("terms", [])
    plot_entries = plot.get("entries", [])

    all_logs: list[str] = []

    # 各deltaを適用（検証を含む）
    try:
        cl = _apply_chars_delta(chars_list, delta.get("characters_delta", []))
        tl = _apply_terms_delta(terms_list, delta.get("terms_delta", []))
        pl = _apply_plot_delta(plot_entries, delta.get("plot_delta", []))
    except Exception as e:
        return False, [str(e)]

    all_logs.extend(cl)
    all_logs.extend(tl)
    all_logs.extend(pl)

    # All-or-nothing: ここで書き出す
    chars["characters"] = chars_list
    terms["terms"] = terms_list
    plot["entries"] = plot_entries

    chars_path = _analysis_path(work, "characters")
    terms_path = _analysis_path(work, "terms")
    plot_path = _analysis_path(work, "plot")

    save_toml(chars_path, chars)
    save_toml(terms_path, terms)
    save_toml(plot_path, plot)

    return True, all_logs


def _analysis_path(work: Path, kind: str) -> Path:
    """書き込み先の分析ファイルパス。"""
    adir = work / "analysis"
    if kind == "characters":
        for name in ("characters-[ネタバレあり].toml", "characters.toml"):
            p = adir / name
            if p.exists():
                return p
        return adir / "characters-[ネタバレあり].toml"
    elif kind == "terms":
        for name in ("terms.toml",):
            p = adir / name
            if p.exists():
                return p
        return adir / "terms.toml"
    elif kind == "plot":
        for name in ("plot-[ネタバレあり].toml", "plot.toml"):
            p = adir / name
            if p.exists():
                return p
        return adir / "plot-[ネタバレあり].toml"
    raise ValueError(f"unknown analysis kind: {kind}")


def _acquire_lock(work: Path) -> bool:
    """排他ロック取得。失敗なら False。"""
    lock_dir = work / "_work"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / ".lock"
    try:
        fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        # stale 判定: mtime が10分超なら上書き
        try:
            st = lock_path.stat()
            age = time.time() - st.st_mtime
            if age > 600:
                lock_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
                return True
        except OSError:
            pass
        return False


def _release_lock(work: Path) -> None:
    lock_path = work / "_work" / ".lock"
    try:
        if lock_path.exists():
            lock_path.unlink()
    except OSError:
        pass


def _ensure_block_field(meta: dict, block_key: str, field: str, value: str) -> None:
    """meta の該当 block に field = value を設定（冪等: 既存ならスキップ）。"""
    for ch in meta.get("chapters", []):
        cid = ch["id"]
        for b in ch.get("blocks", []):
            bid = b["id"]
            if f"{cid}/{bid}" == block_key:
                if field not in b or not b.get(field):
                    b[field] = value
                return


def cmd_analyze_commit(work: Path, delta_path: Path) -> int:
    if not delta_path.is_file():
        print(f"[analyze-commit] エラー: delta ファイルが見つかりません: {delta_path}", file=sys.stderr)
        return 1

    if not _acquire_lock(work):
        print("[analyze-commit] エラー: 他の書き手がロック中です（終了コード 3）", file=sys.stderr)
        return 3

    try:
        delta = _parse_delta(delta_path)
        block = delta.get("block", "").strip()

        meta = load_chapters_meta(work)
        success, logs = _merge_delta(work, delta)

        if not success:
            print("[analyze-commit] エラー: マージ検証失敗、何も書きませんでした", file=sys.stderr)
            return 1

        # 成功: block の analysis_committed_at を立てる
        if block:
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _ensure_block_field(meta, block, "analysis_committed_at", now)
            save_chapters_meta(work, meta)

        if logs:
            for l in logs:
                print(f"[analyze-commit] {l}")
        print(f"[analyze-commit] 完了: {len(delta.get('characters_delta', [])) + len(delta.get('terms_delta', [])) + len(delta.get('plot_delta', []))} 件マージ（{len([l for l in logs if '→proposed' in l])} 件は proposed に退避）")
        return 0
    finally:
        _release_lock(work)


# ---------------------------------------------------------------------------
# prepare サブコマンド
# ---------------------------------------------------------------------------

def _find_first_undone(work: Path, chapters: list[dict], dst: str) -> tuple[str, str, dict] | None:
    """最初の未確定ブロック（output/<ch>/<bid><suffix>.md が存在しない）を返す。
    戻り値: (cid, bid, block_dict)"""
    suffix = target_suffix(dst)
    for ch in chapters:
        cid = ch["id"]
        out_dir = work / "output" / cid
        for b in ch.get("blocks", []):
            bid = b["id"]
            if not (out_dir / f"{bid}{suffix}.md").exists():
                return cid, bid, b
    return None


def cmd_prepare(work: Path, chapter: str | None, block: str | None) -> int:
    if not work.is_dir():
        print(f"[prepare] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    meta = load_chapters_meta(work)
    chapters = meta.get("chapters", [])
    if not chapters:
        print(f"[prepare] エラー: chapters が空です", file=sys.stderr)
        return 1

    src, dst = direction(work)
    suffix = target_suffix(dst)

    if chapter and block:
        cid, bid = chapter, block
    elif chapter:
        cid = chapter
        ch = next((c for c in chapters if c["id"] == cid), None)
        if ch is None:
            print(f"[prepare] エラー: 章 {cid} が見つかりません", file=sys.stderr)
            return 1
        result = _find_first_undone(work, [ch], dst)
        if result is None:
            print(f"[prepare] 章 {cid} は全ブロック確定済みです", file=sys.stderr)
            return 1
        cid, bid, _ = result
    else:
        result = _find_first_undone(work, chapters, dst)
        if result is None:
            print(f"[prepare] 全ブロック確定済みです", file=sys.stderr)
            return 1
        cid, bid, _ = result

    # コンテキスト生成
    work_dir_obj = work
    (work_dir_obj / "translation_input").mkdir(parents=True, exist_ok=True)
    from build_context import _stage_impl as bc_stage
    bc_stage(work, cid, bid, "translation")

    ctx_path = work / "translation_input" / f"{cid}_{bid}_translation.md"

    # 既知の訳語候補を表示
    chars = load_characters(work)
    term_count = len(chars.get("characters", []))
    terms = _load_terms_list(work)
    vocab_count = len(terms)

    # ブロック情報
    ch_obj = next(c for c in chapters if c["id"] == cid)
    blk = next(b for b in ch_obj["blocks"] if b["id"] == bid)
    s, e = blk["paras"]

    print(f"[prepare] 対象: {cid}/{bid}（paras {s}-{e}, {e - s + 1}段落）")
    print(f"[prepare] コンテキスト: {ctx_path}")
    print(f"[prepare] 既知の訳語候補: {vocab_count}件 / 登場人物: {term_count}人")
    return 0


def _load_terms_list(work: Path) -> list[dict]:
    """terms.toml の terms 配列を返す。"""
    try:
        t = load_terms(work)
        return t.get("terms", [])
    except Exception:
        return []


# ---------------------------------------------------------------------------
# commit サブコマンド
# ---------------------------------------------------------------------------

def _draft_path(work: Path, cid: str, bid: str) -> Path:
    _, dst = direction(work)
    suf = target_suffix(dst)
    return work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"


def _output_path(work: Path, cid: str, bid: str) -> Path:
    _, dst = direction(work)
    suf = target_suffix(dst)
    return work / "output" / cid / f"{bid}{suf}.md"


def cmd_commit(work: Path, chapter: str, block: str,
               model: str, prompt_version: str) -> int:
    if not work.is_dir():
        print(f"[commit] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 排他ロック
    if not _acquire_lock(work):
        print("[commit] エラー: 他の書き手がロック中です（終了コード 3）", file=sys.stderr)
        return 3

    started = time.time()
    cid, bid = chapter, block
    draft_p = _draft_path(work, cid, bid)
    out_p = _output_path(work, cid, bid)

    src, dst = direction(work)

    # 冪等リカバリ: emit 済みなら記録だけ補完
    meta = load_chapters_meta(work)
    is_emitted = False
    for c in meta.get("chapters", []):
        if c["id"] == cid:
            for b in c.get("blocks", []):
                if b["id"] == bid and b.get("status") == "emitted":
                    is_emitted = True
                    break

    if is_emitted and out_p.exists():
        print(f"[commit] {cid}/{bid} は確定済みです。記録だけ補完します")
        _record_translation_meta(work, cid, bid, model, prompt_version)
        elapsed = time.time() - started
        print(f"[commit] {cid}/{bid} 記録補完完了 ({elapsed:.1f}s)")
        _release_lock(work)
        return 0

    if not draft_p.exists():
        print(f"[commit] エラー: ドラフトがありません: {draft_p}", file=sys.stderr)
        _release_lock(work)
        return 1

    # 1) スナップショット
    text = draft_p.read_text(encoding="utf-8")
    commit_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    snap_dir = work / "output" / "_work" / "commit" / f"{ts}_{cid}_{bid}"
    snap_dir.mkdir(parents=True, exist_ok=True)
    snap_draft = snap_dir / "draft.md"
    shutil.copy2(str(draft_p), str(snap_draft))
    print(f"[commit] スナップショット: {snap_draft}  commit_id={commit_id}")

    report: dict[str, object] = {"commit_id": commit_id, "snapshot": str(snap_draft),
                                 "chapter": cid, "block": bid, "stages": {}}

    try:
        # 2) 字形整形（glyph のみ）
        from normalize.zh2ja import shape_glyph
        shaped, applied_list = shape_glyph(text)
        if applied_list:
            items = ", ".join(f"{a['item']}->{a['replacement']}(x{a['count']})" for a in applied_list)
            print(f"[commit] 字形整形: {len(applied_list)}件適用 {items}")
        else:
            print(f"[commit] 字形整形: 適用なし")
        report["stages"]["shape_glyph"] = {"applied": len(applied_list)}

        # 書き戻し
        draft_p.write_text(shaped, encoding="utf-8")

        # 3) align 検査
        from align import check_block, _load_acks
        acks = _load_acks(work)
        findings = check_block(work, cid, bid, acks=acks)
        fatals = [f for f in findings if f.severity == "fatal"]
        confirms = [f for f in findings if f.severity == "confirm"]
        # confirm のうち ack の効いていないものを数える
        dh = hashlib.sha256(draft_p.read_bytes()).hexdigest()[:12]
        unacked_confirms = [
            f for f in confirms
            if not any(
                str(a.get("block", bid)) == bid
                and f.para in (int(p) for p in (a.get("paras") or []))
                and str(a.get("draft_hash", "")) == dh
                for a in acks
            )
        ]
        report["stages"]["align"] = {"fatal": len(fatals), "confirm_unacked": len(unacked_confirms)}

        if fatals:
            for f in fatals:
                print(f"[commit] fatal: P{f.para or '?'} {f.kind}: {f.detail}")
            _write_commit_report(snap_dir, report)
            print(f"[commit] 中止。ドラフトを直して再実行。", file=sys.stderr)
            _release_lock(work)
            return 1

        if unacked_confirms:
            for f in unacked_confirms:
                print(f"[commit] confirm(未ack): P{f.para or '?'} {f.kind}: {f.detail}")
            print(f"[commit] 失敗: confirm {len(unacked_confirms)} 件が未ackです。align.py ack を使って確認してください。", file=sys.stderr)
            _write_commit_report(snap_dir, report)
            _release_lock(work)
            return 1

        print(f"[commit] align: PASS (fatal={len(fatals)}, confirm(未ack)={len(unacked_confirms)})")

        # 4) QA
        from qa_translation import run_qa
        qa_report, items = run_qa(work, cid, bid, draft_p)
        report["stages"]["qa"] = {"pass": qa_report["pass"],
                                  "checks": {k: v["pass"] for k, v in qa_report["checks"].items()}}

        if not qa_report["pass"]:
            for k, v in qa_report["checks"].items():
                if not v["pass"]:
                    print(f"[commit] QA FAIL: {k}: {v}")
            _write_commit_report(snap_dir, report)
            print(f"[commit] 中止。QA 不合格。", file=sys.stderr)
            _release_lock(work)
            return 1

        print(f"[commit] QA: PASS")

        # 5) 残渣検査（zh→ja 方向のみ）
        if src == "zh" and dst == "ja":
            from detect.zh2ja import ResidueConfig, find_zh_vocab

            # house_style の known_terms を読み込む
            known_terms: set[str] = set()
            for house_candidate in [work / "_house" / "house_style.toml",
                                    work.parent / "_house" / "house_style.toml"]:
                try:
                    h_data = _load_toml_safe(house_candidate)
                    if h_data:
                        kt = h_data.get("residue", {}).get("known_terms", [])
                        if isinstance(kt, list):
                            known_terms.update(str(x) for x in kt)
                except Exception:
                    pass

            body_text = "\n".join(t for _, t in items)
            rconfig = ResidueConfig(known_terms=known_terms)
            residue_findings = find_zh_vocab(body_text, config=rconfig)

            # safe_fix があるものを「候補」として表示
            residue_candidates = [f for f in residue_findings if f.safe_fix]
            residue_errors = [f for f in residue_findings if not f.safe_fix]
            report["stages"]["residue"] = {"candidates": len(residue_candidates),
                                           "errors": len(residue_errors)}

            if residue_errors:
                for f in residue_errors:
                    sf = f" → {f.safe_fix}" if f.safe_fix else ""
                    ctx = f.context[:40]
                    print(f"[commit] 残渣: 「{f.value}」{sf} (L{f.line}: {ctx})")
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。残渣 {len(residue_errors)} 件を修正してください。", file=sys.stderr)
                _release_lock(work)
                return 1

            if residue_candidates:
                for f in residue_candidates:
                    print(f"[commit] 残渣(候補): 「{f.value}」→ {f.safe_fix} (L{f.line})")
                # 候補にも safe_fix があるものは修正提案に過ぎないが、放置したまま
                # 確定するのはレビュー完了条件「既知パターンの機械検査の未解決エラー
                # 0件（emit 時）」に反する。候補が残る場合は確定を止め、修正か
                # house_style.toml の known_terms 登録を促す。
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。残渣候補 {len(residue_candidates)} 件が残っています。"
                      f"修正するか house_style.toml の residue.known_terms に登録してください。",
                      file=sys.stderr)
                _release_lock(work)
                return 1

            print(f"[commit] 残渣検査: PASS（候補 0 件）")
            report["stages"]["residue"]["pass"] = True

        elif src == "ja" and dst == "zh":
            from detect.ja2zh import find_ja_residue
            body_text = "\n".join(t for _, t in items)
            ja_residue = find_ja_residue(body_text)
            report["stages"]["residue"] = {"errors": len(ja_residue)}
            if ja_residue:
                for f in ja_residue[:20]:
                    print(f"[commit] 残渣: {f}")
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。残渣 {len(ja_residue)} 件を修正してください。", file=sys.stderr)
                _release_lock(work)
                return 1
            print(f"[commit] 残渣検査: PASS")

        # 6) 確定（emit --archive）
        from qa_translation import emit
        emit(work, cid, bid, items, archive=True)

        out_p = _output_path(work, cid, bid)
        print(f"[commit] 確定: {out_p}")

        # 7) 版記録
        _record_translation_meta(work, cid, bid, model, prompt_version)

        # 8) 次の prepare
        elapsed = time.time() - started
        n_residue = 0
        r_stage = report["stages"].get("residue", {})
        if isinstance(r_stage, dict):
            rc = r_stage.get("candidates", 0)
            n_residue = len(rc) if isinstance(rc, list) else int(rc)
        if src == "ja" and dst == "zh":
            re = r_stage.get("errors", 0)
            n_residue = len(re) if isinstance(re, list) else int(re)

        print(f"[commit] {cid}/{bid} を確定 (commit_id={commit_id}, {elapsed:.1f}s)")
        print(f"[commit] 残渣候補: {n_residue}件")

        meta2 = load_chapters_meta(work)
        next_result = _find_first_undone(work, meta2.get("chapters", []), dst)
        if next_result:
            ncid, nbid, _ = next_result
            (work / "translation_input").mkdir(parents=True, exist_ok=True)
            from build_context import _stage_impl as bc_stage
            bc_stage(work, ncid, nbid, "translation")
            nctx = work / "translation_input" / f"{ncid}_{nbid}_translation.md"
            print(f"[next] {ncid}/{nbid} コンテキスト: {nctx}")
        else:
            print(f"[next] 全ブロック確定済み")

        _write_commit_report(snap_dir, report)
        _release_lock(work)
        return 0

    except Exception as e:
        print(f"[commit] エラー: {e}", file=sys.stderr)
        _write_commit_report(snap_dir, report)
        _release_lock(work)
        return 1
    except BaseException:
        _release_lock(work)
        raise


def _record_translation_meta(work: Path, cid: str, bid: str,
                              model: str, prompt_version: str) -> None:
    """版記録。model/prompt_version はオプション"""
    import argparse
    meta = load_chapters_meta(work)
    rec = {
        "model": model if model else "unknown",
        "prompt_version": prompt_version if prompt_version else "unknown",
        "translated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tokens_in": "unmeasured",
        "tokens_out": "unmeasured",
    }
    for c in meta["chapters"]:
        if c["id"] == cid:
            for b in c["blocks"]:
                if b["id"] == bid:
                    b["translation"] = rec
            save_chapters_meta(work, meta)
            return
    print(f"[meta] 警告: {cid}/{bid} が見つかりません", file=sys.stderr)


def _write_commit_report(snap_dir: Path, report: dict) -> None:
    """commit スナップショットディレクトリに report.json を書く。"""
    import json
    report_p = snap_dir / "report.json"
    report_p.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _load_toml_safe(path: Path) -> dict | None:
    """TOML を安全に読む（無ければ None）。"""
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            import tomllib
            return tomllib.load(f)
    except Exception:
        return None

# ---------------------------------------------------------------------------
# review サブコマンド
# ---------------------------------------------------------------------------


def _list_committed_blocks(work: Path) -> list[tuple[str, str]]:
    """output/<ch>/<bid><suffix>.md が存在する確定ブロックを列挙。"""
    _, dst = direction(work)
    suf = target_suffix(dst)
    blocks: list[tuple[str, str]] = []
    meta = load_chapters_meta(work)
    for ch in meta.get("chapters", []):
        cid = ch["id"]
        out_dir = work / "output" / cid
        for b in ch.get("blocks", []):
            bid = b["id"]
            if (out_dir / f"{bid}{suf}.md").is_file():
                blocks.append((cid, bid))
    return blocks


def _scrape_commit_reports(work: Path, blocks: list[tuple[str, str]]) -> list[dict]:
    """各確定ブロックの report.json を _work/commit/ から収集。"""
    reports: list[dict] = []
    commit_dir = work / "output" / "_work" / "commit"
    if not commit_dir.is_dir():
        return reports
    for cid, bid in blocks:
        # 命名パターン: <timestamp>_<cid>_<bid> を含むサブディレクトリを検索
        for entry in sorted(commit_dir.iterdir()):
            if not entry.is_dir():
                continue
            if not entry.name.endswith(f"_{cid}_{bid}"):
                continue
            rp = entry / "report.json"
            if rp.is_file():
                try:
                    data = json.loads(rp.read_text(encoding="utf-8"))
                    data["_block"] = f"{cid}/{bid}"
                    reports.append(data)
                except Exception:
                    pass
    return reports


def _extract_machine_findings(reports: list[dict]) -> list[dict]:
    """report.json の stages から確認履歴（confirm / residue）を抽出。"""
    findings: list[dict] = []
    for r in reports:
        stages = r.get("stages", {})
        align = stages.get("align", {})
        if isinstance(align, dict):
            confs = align.get("confirm_unacked", 0)
            if isinstance(confs, int) and confs > 0:
                findings.append({
                    "block": r.get("_block", ""),
                    "kind": "confirm未ack",
                    "count": confs,
                })
        residue = stages.get("residue", {})
        if isinstance(residue, dict):
            cands = residue.get("candidates", 0)
            if isinstance(cands, int) and cands > 0:
                findings.append({
                    "block": r.get("_block", ""),
                    "kind": "残渣候補",
                    "count": cands,
                })
            errs = residue.get("errors", 0)
            if isinstance(errs, int) and errs > 0:
                findings.append({
                    "block": r.get("_block", ""),
                    "kind": "残渣エラー",
                    "count": errs,
                })
    return findings


def _find_risk_blocks(work: Path, blocks: list[tuple[str, str]]) -> list[dict]:
    """リスクの高い箇所を列挙:
    - arcs の at_block 開始点
    - address を持つ人物がいるブロック
    - ruby を持つ人物の初出ブロック
    """
    from _lib import load_characters
    from _lib import load_paras
    from _lib import load_chapters_meta

    chars_data = load_characters(work)
    char_list = chars_data.get("characters", [])
    risk_items: list[dict] = []
    target_blocks = set(f"{cid}/{bid}" for cid, bid in blocks)

    for c in char_list:
        name = c.get("name_ja") or c.get("name_src") or "?"
        arcs = c.get("arcs") or []
        for a in arcs:
            at = a.get("at_block", "")
            if at in target_blocks:
                changed = []
                for key in ("speech_style", "first_person", "second_person", "tone", "note"):
                    if a.get(key):
                        changed.append(key)
                risk_items.append({
                    "block": at,
                    "kind": f"{name} arc開始",
                    "detail": ", ".join(changed) if changed else "変化",
                })

        # address を持つ人物がいるブロック（address の対象者がそのブロックに登場する可能性）
        addr = c.get("address") or []
        if addr:
            # リスク: address を持つ人物の初出ブロック（arcs の最初 or 任意の blocks）
            # 簡易的に全ブロックでマーク（address指定は呼称の誤りリスク）
            pass  # 全ブロックにマークすると多すぎるので、arcs または初出で絞る

        # ruby を持つ人物
        ruby = c.get("ruby", "")
        if ruby:
            # 初出ブロック = arcs の最初の at_block があればそれ、なければ最初の対象ブロック
            first_arc = arcs[0] if arcs else None
            at = first_arc.get("at_block", "") if first_arc else ""
            if at and at in target_blocks:
                risk_items.append({
                    "block": at,
                    "kind": f"{name} 初出ルビ",
                    "detail": f"ruby={ruby}",
                })

    # 重複除去
    seen: set[tuple[str, str]] = set()
    deduped: list[dict] = []
    for item in risk_items:
        key = (item["block"], item["kind"])
        if key not in seen:
            seen.add(key)
            deduped.append(item)
    return deduped


def _sample_paragraphs(work: Path, blocks: list[tuple[str, str]],
                        ratio: float, seed: int) -> list[dict]:
    """指定ブロックの段落から ratio の割合でランダム選択。"""
    from _lib import load_paras
    rng = random.Random(seed)
    samples: list[dict] = []
    for cid, bid in blocks:
        try:
            paras_data = load_paras(work, cid)
        except Exception:
            continue
        paras = paras_data.get("paras", [])
        meta = load_chapters_meta(work)
        ch = next((c for c in meta.get("chapters", []) if c["id"] == cid), None)
        if not ch:
            continue
        blk = next((b for b in ch.get("blocks", []) if b["id"] == bid), None)
        if not blk:
            continue
        ps, pe = blk.get("paras", (1, len(paras)))
        block_paras = [p for p in paras if ps <= p.get("n", 0) <= pe]
        n = max(1, int(len(block_paras) * ratio))
        chosen = rng.sample(block_paras, min(n, len(block_paras)))
        for p in chosen:
            samples.append({
                "block": f"{cid}/{bid}",
                "para": p.get("n", 0),
                "text": p.get("text", ""),
            })
    return samples


def _read_dst_output(work: Path, cid: str, bid: str) -> str | None:
    """確定済み訳文を読む。"""
    _, dst = direction(work)
    suf = target_suffix(dst)
    out_p = work / "output" / cid / f"{bid}{suf}.md"
    if out_p.is_file():
        return out_p.read_text(encoding="utf-8")
    return None


def cmd_review(work: Path, wave: int = 1, sample_ratio: float = 0.1,
               json_output: bool = False) -> int:
    """検収用の検収リストを生成。"""
    if not work.is_dir():
        print(f"[review] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 確定ブロック
    blocks = _list_committed_blocks(work)
    if not blocks:
        print(f"[review] エラー: 確定済みブロックがありません", file=sys.stderr)
        return 1

    # 1) 機械の指摘
    reports = _scrape_commit_reports(work, blocks)
    findings = _extract_machine_findings(reports)
    machine_items = findings

    # 2) リスク箇所
    risk_items = _find_risk_blocks(work, blocks)

    # 3) 抜き取り（work.name + wave でシード）
    seed_str = f"{work.name}_wave{wave}"
    seed = abs(hash(seed_str))
    sample_items = _sample_paragraphs(work, blocks, sample_ratio, seed)

    # 出力
    src, dst = direction(work)
    rev_dir = work / "_work"
    rev_dir.mkdir(parents=True, exist_ok=True)
    rev_path = rev_dir / f"review_wave{wave}.md"

    # 検収リストを書く
    rev_lines = [
        f"# 検収リスト wave {wave}",
        f"",
        f"work: {work.name}",
        f"対象ブロック数: {len(blocks)}",
        f"生成: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"",
    ]

    # 機械指摘
    rev_lines.append(f"## 1. 機械の指摘（{len(machine_items)}件）")
    if machine_items:
        for m in machine_items:
            rev_lines.append(f"- {m['block']} {m['kind']}: {m['count']}件")
    else:
        rev_lines.append("（なし）")
    rev_lines.append("")

    # リスク箇所
    rev_lines.append(f"## 2. リスクの高い箇所（{len(risk_items)}件）")
    if risk_items:
        for r in risk_items:
            rev_lines.append(f"- {r['block']} {r['kind']}: {r['detail']}")
    else:
        rev_lines.append("（なし）")
    rev_lines.append("")

    # 抜き取り — 原文と訳文を並べる
    rev_lines.append(f"## 3. 抜き取り（{len(sample_items)}段落）")
    if sample_items:
        for s in sample_items:
            blk = s["block"]
            pn = s["para"]
            rev_lines.append(f"### {blk} P{pn}")
            rev_lines.append("")
            rev_lines.append(f"原文: {s['text']}")
            cid, bid = blk.split("/")
            dst_text = _read_dst_output(work, cid, bid)
            if dst_text:
                # 該当段落を探す
                para_lines = dst_text.splitlines()
                found = None
                for line in para_lines:
                    if line.startswith(f"[P{pn:03d}]") or line.startswith(f"[P{pn}]"):
                        found = line
                        break
                if found:
                    rev_lines.append(f"訳文: {found}")
                else:
                    rev_lines.append(f"訳文: （該当段落なし）")
            else:
                rev_lines.append(f"訳文: （未出力）")
            rev_lines.append("")
    else:
        rev_lines.append("（なし）")
    rev_lines.append("")

    rev_lines.append("---")
    rev_lines.append("機械指摘は commit 時の report.json から抽出。リスク箇所は characters.toml の arcs/address/ruby から。抜き取りは乱数シード（work名+wave）で決定的。")
    rev_lines.append("")

    rev_path.write_text("\n".join(rev_lines), encoding="utf-8")

    if json_output:
        import json as _json
        out = {
            "wave": wave,
            "total_blocks": len(blocks),
            "machine_items": machine_items,
            "risk_items": risk_items,
            "sample_items": sample_items,
            "output_path": str(rev_path),
        }
        print(_json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    # コンソール出力（--json が無い場合のみ）
    def _fmt(items: list[dict], max_show: int = 5) -> str:
        parts = [f"{i['block']} {i.get('kind', i.get('detail', '?'))}" for i in items[:max_show]]
        if len(items) > max_show:
            parts.append("...")
        return ", ".join(parts)

    print(f"[review] wave {wave}: 対象 {len(blocks)} ブロック")
    print(f"[review] 機械指摘: {len(machine_items)}件 ({_fmt(machine_items)})")
    print(f"[review] リスク箇所: {len(risk_items)}件 ({_fmt(risk_items)})")
    print(f"[review] 抜き取り: {len(sample_items)}段落 ({_fmt(sample_items)})")
    print(f"[review] 検収対象ファイル: {rev_path}")

    return 0


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="saga-translator CLI")
    sub = ap.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="進捗確認")
    p_status.add_argument("work", help="作品ディレクトリ")

    # analyze-commit
    p_ac = sub.add_parser("analyze-commit", help="分析差分のコミット")
    p_ac.add_argument("work", help="作品ディレクトリ")
    p_ac.add_argument("--delta", required=True, help="差分TOMLファイル")

    # prepare
    p_prep = sub.add_parser("prepare", help="翻訳コンテキスト準備")
    p_prep.add_argument("work", help="作品ディレクトリ")
    p_prep.add_argument("--chapter", default=None, help="章（例: ch001）")
    p_prep.add_argument("--block", default=None, help="ブロック（例: b2）")

    # review
    p_rev = sub.add_parser("review", help="検収リスト生成")
    p_rev.add_argument("work", help="作品ディレクトリ")
    p_rev.add_argument("--wave", type=int, default=1, help="第N波（既定: 1）")
    p_rev.add_argument("--sample-ratio", type=float, default=0.1, help="抜き取り割合（既定: 0.1）")
    p_rev.add_argument("--json", action="store_true", help="JSON 出力")

    # commit
    p_com = sub.add_parser("commit", help="翻訳ブロックの確定")
    p_com.add_argument("work", help="作品ディレクトリ")
    p_com.add_argument("--chapter", required=True, help="章（例: ch001）")
    p_com.add_argument("--block", required=True, help="ブロック（例: b1）")
    p_com.add_argument("--model", default="unknown", help="翻訳に使ったモデル")
    p_com.add_argument("--prompt-version", default="unknown", help="プロンプトバージョン")

    args = ap.parse_args()

    work = Path(args.work).resolve()

    if args.command == "status":
        raise SystemExit(cmd_status(work))
    elif args.command == "analyze-commit":
        raise SystemExit(cmd_analyze_commit(work, Path(args.delta)))
    elif args.command == "prepare":
        raise SystemExit(cmd_prepare(work, args.chapter, args.block))
    elif args.command == "review":
        raise SystemExit(cmd_review(work, wave=args.wave,
                                     sample_ratio=args.sample_ratio,
                                     json_output=args.json))
    elif args.command == "commit":
        raise SystemExit(cmd_commit(work, args.chapter, args.block,
                                     args.model, args.prompt_version))


if __name__ == "__main__":
    main()