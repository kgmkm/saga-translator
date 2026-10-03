"""saga-translator CLI エントリポイント。ステータス確認と分析コミット。"""
from __future__ import annotations

import argparse
import os
import re
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
# main
# ---------------------------------------------------------------------------

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

    args = ap.parse_args()

    work = Path(args.work).resolve()

    if args.command == "status":
        raise SystemExit(cmd_status(work))
    elif args.command == "analyze-commit":
        raise SystemExit(cmd_analyze_commit(work, Path(args.delta)))


if __name__ == "__main__":
    main()