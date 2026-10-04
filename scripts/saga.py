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
    acquire_lock, atomic_write_text, block_meta, block_src_hash, direction, dump_toml,
    load_chapters_meta, load_characters, load_paras, load_plot, load_structured,
    load_terms, load_toml, read_skill_version, record_timeline, release_lock,
    save_chapters_meta, save_toml, sha12, target_suffix, timed_stage, utf8,
)

PLOT_RE = re.compile(r'^block = "([^"]+)"', re.M)

# スキルルート（scripts/ の親）。prepare のドリフト検査で参照する。
SKILL_ROOT = Path(__file__).resolve().parent.parent

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
    started = time.time()
    rc = _cmd_status_impl(work)
    _record_stage(work, "status", None, None, started, rc,
                  reason="success" if rc == 0 else "other")
    return rc


def _cmd_status_impl(work: Path) -> int:
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


def _analysis_backup_dir(work: Path) -> Path:
    """書き込み失敗時の復元用バックアップを置くディレクトリ。"""
    return work / "_work" / "analysis_backup"


def _build_analysis_payloads(work: Path, delta: dict) -> tuple[list[tuple[Path, dict]] | None, list[str], list[str]]:
    """差分を適用した書き込み対象（パス, データ）を組み立てる。書き込みは行わない。

    戻り値 ``(payloads, logs, errors)``:

    - ``payloads``: 成功時のみ ``[(path, data), ...]``。失敗時は ``None``。
    - ``logs``: 適用ログ（proposed 退避など）
    - ``errors``: 検証・構築に失敗した理由（成功時は空）

    反映対象（characters / terms / plot）を**すべて先に組み立て・検証**する。
    ここで例外が出れば一切書き込まずに ``payloads=None`` を返す。
    """
    try:
        chars = load_characters(work)
        terms = load_terms(work)
        plot = load_plot(work)
    except Exception as e:  # 壊れた既存 TOML 等 → 何も書かない
        return None, [], [str(e)]

    chars_list = chars.get("characters", [])
    terms_list = terms.get("terms", [])
    plot_entries = plot.get("entries", [])

    all_logs: list[str] = []
    try:
        cl = _apply_chars_delta(chars_list, delta.get("characters_delta", []))
        tl = _apply_terms_delta(terms_list, delta.get("terms_delta", []))
        pl = _apply_plot_delta(plot_entries, delta.get("plot_delta", []))
    except Exception as e:
        return None, [], [str(e)]

    all_logs.extend(cl)
    all_logs.extend(tl)
    all_logs.extend(pl)

    chars["characters"] = chars_list
    terms["terms"] = terms_list
    plot["entries"] = plot_entries

    payloads = [
        (_analysis_path(work, "characters"), chars),
        (_analysis_path(work, "terms"), terms),
        (_analysis_path(work, "plot"), plot),
    ]
    return payloads, all_logs, []


def _commit_analysis_payloads(payloads: list[tuple[Path, dict]],
                              work: Path) -> list[str]:
    """``[(path, data)]`` を書き込む。失敗時は書き込み済みを元バイト列に復元する。

    戻り値はエラーメッセージのリスト（空ならすべて成功）。復元できなかった
    ファイルがあれば、その旨とバックアップパスをメッセージに含める。
    """
    # 元のバイト列を退避（存在しないファイルは None として記録）
    originals: list[tuple[Path, bytes | None]] = []
    for path, _ in payloads:
        try:
            originals.append((path, path.read_bytes() if path.is_file() else None))
        except OSError:
            originals.append((path, None))

    backup_dir = _analysis_backup_dir(work)
    written: list[Path] = []
    for path, data in payloads:
        try:
            save_toml(path, data)
            written.append(path)
        except Exception as e:
            # ここまでに書いたファイルを元に戻す。まだ書いていないファイルは無傷。
            unsaved = ", ".join(str(p) for p, _ in payloads if p not in written)
            msgs = [f"書き込み失敗: {path.name}: {e}"]
            if unsaved:
                msgs.append(f"  未書き込み（無変更）: {unsaved}")
            for wpath in written:
                orig = next(b for p, b in originals if p == wpath)
                try:
                    if orig is None:
                        if wpath.exists():
                            wpath.unlink()
                    else:
                        wpath.write_bytes(orig)
                    msgs.append(f"  復元: {wpath.name}")
                except OSError as re:
                    # 復元できない場合はバックアップを残して明示する
                    try:
                        backup_dir.mkdir(parents=True, exist_ok=True)
                        for p, b in originals:
                            if b is not None:
                                (backup_dir / p.name).write_bytes(b)
                        msgs.append(
                            f"  復元失敗: {wpath.name}（{re}）。"
                            f"元の内容のバックアップ: {backup_dir}")
                    except OSError:
                        msgs.append(
                            f"  復元失敗: {wpath.name}（{re}）。バックアップも作成できませんでした")
            return msgs
    return []


def _merge_delta(work: Path, delta: dict) -> tuple[bool, list[str]]:
    """差分を3ファイルにマージ。(成功, ログ)

    All-or-nothing: 3ファイルの内容をすべて先に組み立て・検証し、書き込みは
    最後に行う。書き込み途中で失敗した場合は、書き込み済みファイルを元の
    バイト列に復元してから失敗を返す（部分状態を残さない）。
    """
    payloads, logs, errors = _build_analysis_payloads(work, delta)
    if payloads is None:
        return False, errors

    write_errors = _commit_analysis_payloads(payloads, work)
    if write_errors:
        return False, write_errors

    return True, logs


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


def _acquire_lock(work: Path, stage: str = "") -> bool:
    """共有ロックを取得する（_lib に一元化）。取得できなければ False。

    stage はロックファイルに記録する工程名（省略可）。
    """
    ok, _reason = acquire_lock(work, stage)
    return ok


def _acquire_lock_reason(work: Path, stage: str = "") -> tuple[bool, str | None]:
    """共有ロック取得。失敗理由も返す（exit 3 のメッセージ用）。"""
    return acquire_lock(work, stage)


def _release_lock(work: Path) -> None:
    """自分のロックのみ解放する（_lib に一元化）。"""
    release_lock(work)


def _ensure_block_field(meta: dict, block_key: str, field: str, value) -> None:
    """meta の該当 block に field = value を設定（冪等: 既存ならスキップ）。"""
    for ch in meta.get("chapters", []):
        cid = ch["id"]
        for b in ch.get("blocks", []):
            bid = b["id"]
            if f"{cid}/{bid}" == block_key:
                if field not in b or not b.get(field):
                    b[field] = value
                return


def _set_block_field(meta: dict, block_key: str, field: str, value) -> None:
    """meta の該当 block に field = value を無条件で設定（上書き）。"""
    for ch in meta.get("chapters", []):
        cid = ch["id"]
        for b in ch.get("blocks", []):
            bid = b["id"]
            if f"{cid}/{bid}" == block_key:
                b[field] = value
                return


def _analysis_src_hash(work: Path, block_key: str, meta: dict) -> str:
    """block 原文（paragraphs）の sha256 先頭12桁。原文が変われば値が変わる。

    原文が読めない場合は空文字を返す（旧データ互換: hash 未記録を許容）。
    """
    b = block_meta(meta, block_key)
    if b is None:
        return ""
    cid = block_key.split("/", 1)[0]
    try:
        paras_data = load_paras(work, cid)
    except (FileNotFoundError, OSError, ValueError):
        return ""
    return block_src_hash(paras_data, b)


def validate_analysis_delta(meta: dict, delta: dict) -> tuple[bool, str | None, bool]:
    """analyze-commit の受入検証（純関数・I/O 無し）。

    戻り値 ``(ok, reason, has_changes)``:
    - ``ok``: 受理してマージ・記録してよいか
    - ``reason``: 拒否理由（ok=True なら None）
    - ``has_changes``: 非空の差分（characters/terms/plot いずれか）があるか

    判定順:
    1. ``block`` 必須
    2. ``block`` が chapters_meta に存在（``chNNN/bN`` 形式）
    3. スキーマ検証（型不一致は拒否。block 以外のキーは無視）
    4. plot エントリの ``block`` は delta の block と一致
    5. 差分が空なら ``no_changes = true`` の明示が必要
    """
    block = delta.get("block")
    if not isinstance(block, str) or not block.strip():
        return False, "delta に必須キー `block` がありません（ブロック単位で分析すること）", False
    block = block.strip()
    if block_meta(meta, block) is None:
        return False, f"`block = \"{block}\"` は chapters_meta に存在しません（未知のブロック）", False

    def _str_field(entry: dict, key: str) -> bool:
        return isinstance(entry.get(key), str) and entry[key].strip() != ""

    for key in ("characters_delta", "terms_delta", "plot_delta"):
        if not isinstance(delta.get(key, []), list):
            return False, f"`{key}` は配列（[[{key}]]）であるべきです", False

    for i, c in enumerate(delta.get("characters_delta", [])):
        if not isinstance(c, dict):
            return False, f"characters_delta[{i}] はテーブル（配列ofテーブル）であるべきです", False
        for key in ("match_key", "match_value"):
            if not _str_field(c, key):
                return False, f"characters_delta[{i}] に必須キー `{key}`（文字列）がありません", False
    for i, t in enumerate(delta.get("terms_delta", [])):
        if not isinstance(t, dict):
            return False, f"terms_delta[{i}] はテーブル（配列ofテーブル）であるべきです", False
        for key in ("match_key", "match_value"):
            if not _str_field(t, key):
                return False, f"terms_delta[{i}] に必須キー `{key}`（文字列）がありません", False
    for i, p in enumerate(delta.get("plot_delta", [])):
        if not isinstance(p, dict):
            return False, f"plot_delta[{i}] はテーブル（配列ofテーブル）であるべきです", False
        if not _str_field(p, "text"):
            return False, f"plot_delta[{i}] に必須キー `text`（文字列）がありません", False
        pb = p.get("block")
        if not isinstance(pb, str) or pb.strip() != block:
            return False, (f"plot_delta[{i}] の `block` が delta の block と一致しません"
                           f"（対象外ブロックの plot は拒否: {pb!r} != {block!r}）"), False

    has_changes = any(len(delta.get(k, [])) > 0
                      for k in ("characters_delta", "terms_delta", "plot_delta"))
    if not has_changes and delta.get("no_changes") is not True:
        return False, ("差分が空です。分析の結果として変更が無い場合は `no_changes = true` を"
                       "明示してください（黙っての空コミットは拒否）"), False

    return True, None, has_changes


def cmd_analyze_commit(work: Path, delta_path: Path) -> int:
    started = time.time()
    rc = _cmd_analyze_commit_impl(work, delta_path)
    reason = "success" if rc == 0 else "other"
    _record_stage(work, "analyze-commit", None, None, started, rc, reason=reason)
    return rc


def _cmd_analyze_commit_impl(work: Path, delta_path: Path) -> int:
    if not delta_path.is_file():
        print(f"[analyze-commit] エラー: delta ファイルが見つかりません: {delta_path}", file=sys.stderr)
        return 1

    ok, lock_reason = _acquire_lock_reason(work, "analyze-commit")
    if not ok:
        print(f"[analyze-commit] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        return 3

    try:
        delta = _parse_delta(delta_path)
        block = delta.get("block", "").strip()

        meta = load_chapters_meta(work)

        # 受入検証（_merge_delta の前に実施。拒否時は一切書かない）
        ok, reason, has_changes = validate_analysis_delta(meta, delta)
        if not ok:
            print(f"[analyze-commit] エラー: 拒否しました: {reason}", file=sys.stderr)
            return 1

        # 差分が空（no_changes 明示）のときは analysis ファイルに触れない
        if has_changes:
            success, logs = _merge_delta(work, delta)
            if not success:
                print("[analyze-commit] エラー: マージ検証失敗、何も書きませんでした", file=sys.stderr)
                return 1
        else:
            logs = []

        # 成功: block の「分析済み」を記録する
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        _ensure_block_field(meta, block, "analysis_committed_at", now)
        _set_block_field(
            meta, block, "analysis_src_hash",
            _analysis_src_hash(work, block, meta))

        if has_changes:
            # 変更が反映された → 変更なしフラグは残さない
            b = block_meta(meta, block)
            if b is not None:
                b.pop("analysis_no_changes", None)
        else:
            # 変更なしの明示的な記録
            _set_block_field(meta, block, "analysis_no_changes", True)

        save_chapters_meta(work, meta)

        if logs:
            for l in logs:
                print(f"[analyze-commit] {l}")
        n = (len(delta.get("characters_delta", [])) + len(delta.get("terms_delta", []))
             + len(delta.get("plot_delta", [])))
        if has_changes:
            print(f"[analyze-commit] 完了: {n} 件マージ（{len([l for l in logs if '→proposed' in l])} 件は proposed に退避）")
        else:
            print("[analyze-commit] 完了: 差分なし（analysis_no_changes = true を記録）")
        return 0
    finally:
        _release_lock(work)


# ---------------------------------------------------------------------------
# prepare サブコマンド
# ---------------------------------------------------------------------------

def _record_stage(work: Path, stage: str, chapter: str | None, block: str | None,
                  started: float, exit_code: int, reason: str | None = None,
                  mode: str | None = None) -> None:
    """工程の所要時間を timeline.jsonl に1行記録する（成功・失敗の両方）。

    started は time.time() の値。record_timeline が失敗しても本処理は止めない。
    """
    record_timeline(
        work, stage, chapter=chapter or None, block=block or None,
        exit_code=exit_code, reason=reason, mode=mode,
        started=datetime.fromtimestamp(started, tz=timezone.utc),
        ended=datetime.now(timezone.utc),
    )


# 失敗理由の分類（commit の検査工程の順序に合わせる）。
# 値を増やすと commit の timeline に載る reason の種類が増える。
COMMIT_FAIL_REASONS = (
    "success",           # 成功
    "align",             # align fatal（会話頭・欠落・重複マーカー等）
    "confirm_unacked",   # confirm 未 ack
    "qa",                # QA 不合格
    "merged_paragraphs",  # QA のうち段落結合
    "non_jis",           # QA のうち CP932 外漢字の残存
    "residue",           # 残渣（エラー/候補）
    "draft_missing",     # ドラフト不在
    "locked",            # 他 writer がロック中
    "exception",         # 想定外の例外
    "other",             # 分類不能
)


def _classify_commit_failure(snap_dir: Path) -> str:
    """commit 失敗時の理由を種類別に分類する。

    検査ごとの検出数を記録した report.json を優先して読み、無ければ None。
    分類記号は COMMIT_FAIL_REASONS のいずれか。
    """
    report_p = Path(snap_dir) / "report.json"
    if not report_p.is_file():
        return "other"
    try:
        report = json.loads(report_p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "other"
    stages = report.get("stages", {})
    align = stages.get("align", {})
    if isinstance(align, dict) and int(align.get("fatal", 0) or 0) > 0:
        return "align"
    if isinstance(align, dict) and int(align.get("confirm_unacked", 0) or 0) > 0:
        return "confirm_unacked"
    qa = stages.get("qa")
    if isinstance(qa, dict) and not qa.get("pass", True):
        checks = qa.get("checks", {})
        if isinstance(checks, dict):
            if checks.get("merged_paragraphs") is False:
                return "merged_paragraphs"
            if checks.get("non_jis_han") is False:
                return "non_jis"
        return "qa"
    residue = stages.get("residue", {})
    if isinstance(residue, dict) and (int(residue.get("errors", 0) or 0) > 0
                                      or int(residue.get("candidates", 0) or 0) > 0):
        return "residue"
    return "other"


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


def cmd_prepare(work: Path, chapter: str | None, block: str | None,
                mode: str = "translation") -> int:
    started = time.time()
    outcome: dict = {"cid": chapter, "bid": block}
    rc = _cmd_prepare_impl(work, chapter, block, mode, outcome)
    _record_stage(work, "prepare", outcome["cid"], outcome["bid"], started, rc, mode=mode)
    return rc


def _cmd_prepare_impl(work: Path, chapter: str | None, block: str | None,
                      mode: str, outcome: dict) -> int:
    if not work.is_dir():
        print(f"[prepare] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 排他ロック（共有ロックに一元化。解放は finally で必ず行う）
    ok, lock_reason = _acquire_lock_reason(work, "prepare")
    if not ok:
        print(f"[prepare] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        return 3
    try:
        return _cmd_prepare_locked(work, chapter, block, mode, outcome)
    finally:
        _release_lock(work)


def _cmd_prepare_locked(work: Path, chapter: str | None, block: str | None,
                        mode: str, outcome: dict) -> int:
    # スキル版ドリフト警告: 記録された skill_version と実行中の版が違えば知らせる
    # （非致命。記録が無い旧作業ディレクトリでは警告しない）。
    try:
        man = load_structured(work / "manifest")
    except (FileNotFoundError, OSError):
        man = {}
    recorded = man.get("skill_version")
    running = read_skill_version(SKILL_ROOT)
    if recorded and running and str(recorded) != str(running):
        print(
            f"[prepare] 警告: スキル版が作業ディレクトリ作成時と異なります"
            f"（記録: {recorded} / 実行中: {running}）。挙動差に注意してください",
            file=sys.stderr,
        )

    meta_p = work / "chapters_meta.toml"
    if not meta_p.is_file():
        meta_p = work / "chapters_meta.json"
    if not meta_p.is_file():
        # ゼロ状態（ingest 済み・split 未実行）は未初期化であって構文エラーではない。
        # Traceback を出さず、chapters が空の案内だけで終了する。
        print(f"[prepare] エラー: chapters が空です（split_chapters.py 未実行の可能性）", file=sys.stderr)
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

    outcome["cid"], outcome["bid"] = cid, bid

    # コンテキスト生成
    work_dir_obj = work
    (work_dir_obj / "translation_input").mkdir(parents=True, exist_ok=True)
    from build_context import _stage_impl as bc_stage
    bc_stage(work, cid, bid, mode)

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
               model: str, prompt_version: str, revise: bool = False) -> int:
    """commit を実行し、成功/失敗いずれでも timeline.jsonl に1行記録する。"""
    started = time.time()
    outcome: dict = {"reason": "other", "commit_id": None, "snap_dir": None}
    rc = _cmd_commit_impl(work, chapter, block, model, prompt_version, revise, outcome)
    # 失敗理由は種類別に分類（成功は success）。chapter/block は指定値をそのまま記録。
    if outcome["snap_dir"] is not None:
        reason = _classify_commit_failure(outcome["snap_dir"])
    else:
        reason = outcome["reason"]
    if rc == 0:
        reason = "success"
    record_timeline(
        work, "commit", chapter=chapter or None, block=block or None,
        exit_code=rc, reason=reason, commit_id=outcome["commit_id"],
        started=datetime.fromtimestamp(started, tz=timezone.utc),
        ended=datetime.now(timezone.utc),
    )
    return rc


def _cmd_commit_impl(work: Path, chapter: str, block: str,
                     model: str, prompt_version: str, revise: bool,
                     outcome: dict) -> int:
    if not work.is_dir():
        print(f"[commit] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 排他ロック（共有ロックに一元化。解放は finally で必ず行う）
    ok, lock_reason = _acquire_lock_reason(work, "commit")
    if not ok:
        print(f"[commit] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        outcome["reason"] = "locked"
        return 3

    try:
        rc = _finalize_block(work, chapter, block, model, prompt_version,
                             revise=revise, archive=True, outcome=outcome)
        if rc != 0:
            return rc
    finally:
        # 正常・例外いずれでも自分のロックを必ず解放する。
        _release_lock(work)

    # 確定後の次コンテキスト生成は「確定の外」（ロック解放後）。ここで失敗しても確定は
    # 既に success。exit 0 を保ち、警告だけを出す（次回 prepare で再試行できる）。
    # 復旧（記録の補完）だけの再実行では次コンテキストを作り直さない。
    if outcome.get("finalized"):
        _, dst = direction(work)
        _generate_next_context(work, dst)

    return 0


def _config_hash(work: Path) -> str:
    """検査設定（house_style.toml 等）の版を表す sha256 先頭12桁。

    確定記録の config_hash に使う（dict_version と同義の用途）。house_style が
    無い場合も「無し」を表す決定的な値になる（探索先の中身が変われば値が変わる）。
    """
    parts: list[str] = []
    for label, path in (("work", work / "_house" / "house_style.toml"),
                        ("parent", work.parent / "_house" / "house_style.toml")):
        try:
            data = path.read_bytes() if path.is_file() else b""
        except OSError:
            data = b""
        parts.append(f"{label}:{sha12(data)}")
    return sha12("|".join(parts).encode("utf-8"))


def _hashes_for_block(work: Path, meta: dict, block_key: str,
                      draft_hash: str, final_hash: str) -> dict[str, str]:
    """確定記録に残す由来のハッシュ（sha256 先頭12桁）を組み立てる。

    src_hash: 原文（現在の paragraphs）/ analysis_src_hash: 分析記録の値 /
    config_hash: 検査設定の版 / draft_hash: 正規化後ドラフト / final_hash: 確定出力
    """
    b = block_meta(meta, block_key) or {}
    return {
        "src_hash": _analysis_src_hash(work, block_key, meta),
        "analysis_src_hash": str(b.get("analysis_src_hash") or ""),
        "config_hash": _config_hash(work),
        "draft_hash": str(draft_hash or ""),
        "final_hash": str(final_hash or ""),
    }


def _translation_record(work: Path, meta: dict, block_key: str,
                        draft_hash: str, final_hash: str,
                        model: str | None, prompt_version: str | None,
                        tokens_in, tokens_out) -> dict:
    """chapters_meta の block.translation に書く確定記録（由来ハッシュ付き）。

    model / prompt_version / translated_at / tokens_in / tokens_out の既存キーは
    そのまま残し、由来のハッシュを追加する（既存キーを壊さない）。
    """
    rec: dict = {
        "model": model if model else "unknown",
        "prompt_version": prompt_version if prompt_version else "unknown",
        "translated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tokens_in": tokens_in if tokens_in is not None else "unmeasured",
        "tokens_out": tokens_out if tokens_out is not None else "unmeasured",
    }
    rec.update(_hashes_for_block(work, meta, block_key, draft_hash, final_hash))
    return rec


def _complete_emitted_record(work: Path, meta: dict, cid: str, bid: str, suf: str,
                             model: str | None, prompt_version: str | None,
                             tokens_in, tokens_out) -> bool:
    """確定済みブロックの「途中の段階」を完遂する（#16 の復旧）。

    emit 済みなのに中断で記録が欠けている場合は、残った成果物（_work/done/ の
    ドラフトと確定出力）から分かる範囲で記録を補完する。既にある由来の記録は
    上書きしない。何か変更したら True を返す。
    """
    key = f"{cid}/{bid}"
    b = block_meta(meta, key)
    if b is None:
        return False
    changed = False

    tr = b.get("translation")
    if not isinstance(tr, dict) or not tr:
        done_p = work / "output" / "_work" / "done" / f"{cid}_{bid}{suf}_numbered.md"
        draft_hash = sha12(done_p.read_bytes()) if done_p.is_file() else ""
        out_p = _output_path(work, cid, bid)
        final_hash = sha12(out_p.read_bytes()) if out_p.is_file() else ""
        b["translation"] = _translation_record(work, meta, key, draft_hash, final_hash,
                                               model, prompt_version, tokens_in, tokens_out)
        changed = True

    if b.get("status") != "emitted":
        b["status"] = "emitted"
        if not b.get("emitted_at"):
            b["emitted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        changed = True

    for c in meta.get("chapters", []):
        if c.get("id") == cid:
            blocks = c.get("blocks", [])
            if blocks and all(x.get("status") == "emitted" for x in blocks):
                if c.get("status") != "done":
                    c["status"] = "done"
                    changed = True
            break
    return changed


def _finalize_block(work: Path, chapter: str, block: str,
                    model: str | None = None, prompt_version: str | None = None,
                    revise: bool = False, archive: bool = True,
                    tokens_in=None, tokens_out=None,
                    outcome: dict | None = None) -> int:
    """共通の確定処理（#17）。「検査（align / QA / 残渣 / 非JIS / 会話頭）→ emit → 記録」。

    検査・emit・記録は、検査開始時に取得した正規化済みドラフトのバイト列
    スナップショット（output/_work/commit/<ts>_<cid>_<bid>/draft.md）に対して行う（#16）。
    必要な正規化（字形整形）は検査前に一括適用する。

    確定済みブロックへの再実行は、既定では「復旧」＝途中の段階の完遂だけを行い、
    由来の記録を上書きしない（未使用の新ドラフトがあれば --revise を案内する）。
    `revise=True` のときだけ新しいドラフトで改訂し、旧記録を translation_history
    に残して新しい記録（revised_at / prev_draft_hash 付き）へ更新する。

    commit / safe_emit / qa_translation --emit はすべてこの関数を通る。
    呼び出し側が共有ロック（_lib.acquire_lock）を保持している前提。
    戻り値は終了コード（0=確定または復旧の完遂、非0=検査で停止）。
    """
    out = outcome if outcome is not None else {}
    out.setdefault("reason", "other")
    out.setdefault("commit_id", None)
    out.setdefault("snap_dir", None)
    out["finalized"] = False

    cid, bid = chapter, block
    started = time.time()
    try:
        meta = load_chapters_meta(work)
    except Exception as e:
        print(f"[commit] エラー: {e}", file=sys.stderr)
        out["reason"] = "exception"
        return 1
    if block_meta(meta, f"{cid}/{bid}") is None:
        print(f"[commit] エラー: {cid}/{bid} は chapters_meta に存在しません", file=sys.stderr)
        out["reason"] = "other"
        return 1

    src, dst = direction(work)
    suf = target_suffix(dst)
    draft_p = _draft_path(work, cid, bid)
    out_p = _output_path(work, cid, bid)

    # ---- 確定済み: 復旧（既定）と改訂（--revise 明示）を分ける（#16）----
    if out_p.is_file() and not revise:
        changed = _complete_emitted_record(work, meta, cid, bid, suf,
                                           model, prompt_version, tokens_in, tokens_out)
        if changed:
            save_chapters_meta(work, meta)
        if draft_p.is_file():
            print(f"[commit] {cid}/{bid} は確定済みです。新しいドラフトは使わず、由来の記録も上書きしません")
            print(f"[commit] 改訂するには --revise を付けて再実行してください: saga.py commit --revise")
        elif changed:
            print(f"[commit] {cid}/{bid} は確定済みです。記録を補完しました")
        else:
            print(f"[commit] {cid}/{bid} は確定済みです（記録に変更はありません）")
        out["reason"] = "success"
        return 0

    if not draft_p.is_file():
        if revise:
            print(f"[commit] エラー: 改訂には新しいドラフトが必要です: {draft_p}", file=sys.stderr)
        else:
            print(f"[commit] エラー: ドラフトがありません: {draft_p}", file=sys.stderr)
        out["reason"] = "draft_missing"
        return 1

    snap_dir: Path | None = None
    report: dict | None = None
    try:
        # 1) 正規化は検査前に一括適用し、その結果をスナップショットにする（#16）
        text = draft_p.read_text(encoding="utf-8")
        if (src, dst) == ("zh", "ja"):
            from normalize.zh2ja import shape_glyph
        elif (src, dst) == ("ja", "zh"):
            from normalize.ja2zh import shape_glyph
        else:
            def shape_glyph(text, **_kw):
                return text, []

        shaped, applied_list = shape_glyph(text)
        if applied_list:
            items_s = ", ".join(f"{a['item']}->{a['replacement']}(x{a['count']})" for a in applied_list)
            print(f"[commit] 字形整形: {len(applied_list)}件適用 {items_s}")
        else:
            print(f"[commit] 字形整形: 適用なし")

        # 正規化の結果をドラフトへ反映する。以降の検査・emit・記録はこのバイト列で行う。
        draft_p.write_text(shaped, encoding="utf-8")
        snap_bytes = draft_p.read_bytes()
        draft_hash = sha12(snap_bytes)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        snap_dir = work / "output" / "_work" / "commit" / f"{ts}_{cid}_{bid}"
        snap_dir.mkdir(parents=True, exist_ok=True)
        snap_draft = snap_dir / "draft.md"
        snap_draft.write_bytes(snap_bytes)
        out["snap_dir"] = snap_dir
        print(f"[commit] スナップショット: {snap_draft}  commit_id={draft_hash}")

        report = {"commit_id": draft_hash, "snapshot": str(snap_draft),
                  "chapter": cid, "block": bid, "revise": bool(revise), "stages": {}}

        # 2) align 検査（会話頭・欠落・重複マーカー）
        from align import check_block, _load_acks
        acks = _load_acks(work)
        findings = check_block(work, cid, bid, acks=acks)
        fatals = [f for f in findings if f.severity == "fatal"]
        confirms = [f for f in findings if f.severity == "confirm"]
        # confirm のうち ack の効いていないものを数える（ack は正規化後ドラフトの
        # バイト列ハッシュに紐づく）
        unacked_confirms = [
            f for f in confirms
            if not any(
                str(a.get("block", bid)) == bid
                and f.para in (int(p) for p in (a.get("paras") or []))
                and str(a.get("draft_hash", "")) == draft_hash
                for a in acks
            )
        ]
        report["stages"]["align"] = {"fatal": len(fatals), "confirm_unacked": len(unacked_confirms)}

        if fatals:
            for f in fatals:
                print(f"[commit] fatal: P{f.para or '?'} {f.kind}: {f.detail}")
            _write_commit_report(snap_dir, report)
            print(f"[commit] 中止。ドラフトを直して再実行。", file=sys.stderr)
            out["reason"] = "align"
            return 1

        if unacked_confirms:
            for f in unacked_confirms:
                print(f"[commit] confirm(未ack): P{f.para or '?'} {f.kind}: {f.detail}")
            print(f"[commit] 失敗: confirm {len(unacked_confirms)} 件が未ackです。align.py ack を使って確認してください。", file=sys.stderr)
            _write_commit_report(snap_dir, report)
            out["reason"] = "confirm_unacked"
            return 1

        print(f"[commit] align: PASS (fatal={len(fatals)}, confirm(未ack)={len(unacked_confirms)})")

        # 3) QA（非JIS・段落整合など）。検査対象はスナップショットと同じバイト列。
        from qa_translation import run_qa
        qa_report, items = run_qa(work, cid, bid, snap_draft)
        report["stages"]["qa"] = {"pass": qa_report["pass"],
                                  "checks": {k: v["pass"] for k, v in qa_report["checks"].items()}}

        if not qa_report["pass"]:
            for k, v in qa_report["checks"].items():
                if not v["pass"]:
                    print(f"[commit] QA FAIL: {k}: {v}")
            _write_commit_report(snap_dir, report)
            print(f"[commit] 中止。QA 不合格。", file=sys.stderr)
            out["reason"] = "qa"
            return 1

        print(f"[commit] QA: PASS")

        # 4) 残渣検査（zh→ja 方向のみ）
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
                out["reason"] = "residue"
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
                out["reason"] = "residue"
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
                out["reason"] = "residue"
                return 1
            print(f"[commit] 残渣検査: PASS")

        # 5) 確定（emit）— ここまでが「確定」。成功したら exit 0 を確定させる。
        from qa_translation import emit
        emit(work, cid, bid, items, archive=archive)

        out_p = _output_path(work, cid, bid)
        final_hash = sha12(out_p.read_bytes())
        print(f"[commit] 確定: {out_p}")

        # 6) 記録（由来のハッシュ。改訂時は旧記録を履歴に残す）
        meta2 = load_chapters_meta(work)
        b2 = block_meta(meta2, f"{cid}/{bid}")
        rec = _translation_record(work, meta2, f"{cid}/{bid}", draft_hash, final_hash,
                                  model, prompt_version, tokens_in, tokens_out)
        if b2 is not None:
            if revise:
                old = b2.get("translation")
                if isinstance(old, dict) and old:
                    hist = b2.get("translation_history")
                    if not isinstance(hist, list):
                        hist = []
                    hist.append(old)
                    b2["translation_history"] = hist
                    rec["prev_draft_hash"] = str(old.get("draft_hash") or "")
                    rec["revised_at"] = rec["translated_at"]
            b2["translation"] = rec
            save_chapters_meta(work, meta2)

        report["hashes"] = {k: rec[k] for k in
                            ("src_hash", "analysis_src_hash", "config_hash",
                             "draft_hash", "final_hash")}

        elapsed = time.time() - started
        n_residue = 0
        r_stage = report["stages"].get("residue", {})
        if isinstance(r_stage, dict):
            cnt = r_stage.get("candidates", 0)
            n_residue = len(cnt) if isinstance(cnt, list) else int(cnt)
        if src == "ja" and dst == "zh":
            cnt = r_stage.get("errors", 0)
            n_residue = len(cnt) if isinstance(cnt, list) else int(cnt)

        verb = "改訂" if revise else "確定"
        print(f"[commit] {cid}/{bid} を{verb} (commit_id={draft_hash}, {elapsed:.1f}s)")
        print(f"[commit] 残渣候補: {n_residue}件")

        _write_commit_report(snap_dir, report)
        out["reason"] = "success"
        out["commit_id"] = draft_hash
        out["finalized"] = True
        return 0
    except Exception as e:
        print(f"[commit] エラー: {e}", file=sys.stderr)
        if snap_dir is not None and report is not None:
            _write_commit_report(snap_dir, report)
        out["reason"] = "exception"
        return 1


def _generate_next_context(work: Path, dst: str) -> None:
    """確定後に次の未確定ブロックのコンテキストを生成する（失敗しても確定は壊さない）。

    例外は投げず、失敗時は警告を表示する。reason/exit code は呼び出し側が決める。
    """
    try:
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
    except Exception as e:
        print(f"[commit] 確定しました。次のコンテキスト生成に失敗"
              f"（次回 prepare で再試行できます）: {e}", file=sys.stderr)


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
    started = time.time()
    rc = _cmd_review_impl(work, wave, sample_ratio, json_output)
    _record_stage(work, "review", None, None, started, rc,
                  reason="success" if rc == 0 else "other")
    return rc


def _cmd_review_impl(work: Path, wave: int = 1, sample_ratio: float = 0.1,
                     json_output: bool = False) -> int:
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
    p_prep.add_argument("--mode", choices=["translation", "analysis"], default="translation",
                        help="コンテキストのモード（既定: translation）")

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
    p_com.add_argument("--revise", action="store_true",
                       help="確定済みブロックを新しいドラフトで改訂する（既定は復旧のみ）")

    args = ap.parse_args()

    work = Path(args.work).resolve()

    if args.command == "status":
        raise SystemExit(cmd_status(work))
    elif args.command == "analyze-commit":
        raise SystemExit(cmd_analyze_commit(work, Path(args.delta)))
    elif args.command == "prepare":
        raise SystemExit(cmd_prepare(work, args.chapter, args.block, mode=args.mode))
    elif args.command == "review":
        raise SystemExit(cmd_review(work, wave=args.wave,
                                     sample_ratio=args.sample_ratio,
                                     json_output=args.json))
    elif args.command == "commit":
        raise SystemExit(cmd_commit(work, args.chapter, args.block,
                                     args.model, args.prompt_version,
                                     revise=args.revise))


if __name__ == "__main__":
    main()