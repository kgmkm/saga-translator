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
    VOICE_STATUSES, acquire_lock, atomic_write_text, block_meta, block_paras, block_src_hash,
    character_aliases, derivatives_stale_at, direction, dump_toml, load_chapters_meta,
    load_characters, load_paras,
    load_plot, load_structured, load_terms, load_toml, merge_speaker_candidate,
    merge_variant_candidates, normalize_variant_candidates, read_skill_version, record_timeline,
    release_lock, save_chapters_meta, save_toml, sha12, speaker_candidate_blocks,
    stale_after_rename_blocks, target_suffix,
    timed_stage, utf8, variant_candidates_of, voice_description_present, voice_status_of,
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


def _assembled_state(work: Path) -> tuple[str | None, list[str]]:
    """manifest の assemble 完成記録を読む: (assembled_at, 出力成果物の一覧)。

    assemble が「完成・中間物削除済み」に達した work だけが記録を持つ（#20）。
    記録が無い・読めない work では (None, []) を返し、既存の進捗判定を使う。
    """
    try:
        man = load_structured(work / "manifest")
    except Exception:
        return None, []
    at = man.get("assembled_at")
    outs = man.get("assembled_outputs")
    return (str(at) if at else None), (
        [str(x) for x in outs if x] if isinstance(outs, list) else [])


def _cap_list(items: list[str], cap: int = 8) -> str:
    """一覧の表示（cap 件まで。超過分は …他N件）。"""
    shown = ", ".join(items[:cap])
    if len(items) > cap:
        shown += f" …他{len(items) - cap}件"
    return shown


def _status_pending_analysis(work: Path) -> list[str]:
    """status の「分析の確認待ち」節（P0-4・#7）。

    未確定の声（unconfirmed / insufficient_evidence）と、未解決の話者/異表記の
    確認候補を列挙する。検収済みの「未確定」は許容する（推測で埋めない方針の明示）。
    """
    from build_context import VOICE_STATUS_LABELS
    try:
        chars_data = load_characters(work)
    except Exception as e:
        return [f"[status] 分析の確認待ち: （人物表を読めません: {e}）"]
    chars = [c for c in (chars_data.get("characters") or []) if isinstance(c, dict)]

    voice_wait: list[str] = []
    speaker_wait: list[str] = []
    variant_wait: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()

    def _variant_line(name_src: str, similar_to: str, block: str, suffix: str = "") -> str:
        pair = "⇔".join(sorted((name_src, similar_to))) if similar_to else name_src
        return f"{pair} ({block}){suffix}" if block else f"{pair}{suffix}"

    for c in chars:
        name = c.get("name_ja") or c.get("name_src") or "(無名)"
        status = voice_status_of(c)
        if status in ("unconfirmed", "insufficient_evidence"):
            voice_wait.append(f"{name} ({VOICE_STATUS_LABELS.get(status, status)})")
        blocks = speaker_candidate_blocks(c)
        if blocks:
            speaker_wait.append(f"{name} ({', '.join(blocks)})")
        for cand in variant_candidates_of(c):
            pair = tuple(sorted((cand["name_src"], cand["similar_to_src"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            variant_wait.append(_variant_line(cand["name_src"], cand["similar_to_src"], cand["block"]))
    for cand in normalize_variant_candidates(chars_data.get("variant_candidates")):
        pair = tuple(sorted((cand["name_src"], cand["similar_to_src"])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        variant_wait.append(
            _variant_line(cand["name_src"], cand["similar_to_src"], cand["block"], "（該当キャラなし）"))

    if not voice_wait and not speaker_wait and not variant_wait:
        return ["[status] 分析の確認待ち: なし"]
    out = ["[status] 分析の確認待ち:"]
    if voice_wait:
        out.append("[status]   声の状態(未確認/根拠不足): " + _cap_list(voice_wait))
    if speaker_wait:
        out.append("[status]   話者の確認候補: " + _cap_list(speaker_wait))
    if variant_wait:
        out.append("[status]   異表記候補: " + _cap_list(variant_wait))
    out.append("[status]   （検収済みの「未確定」は許容。推測で埋めず、埋め直しは1周後に delta で1回）")
    return out


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

    assembled_at, assembled_outputs = _assembled_state(work)
    # 改名後の stale（派生物の再生成と検査記録の再検収が必要。P0-6・#22）。
    # 再 assemble の記録が stale より新しければ解消済みとして扱う。
    stale_at = derivatives_stale_at(work)
    stale_active = bool(stale_at) and not (assembled_at and assembled_at > stale_at)
    stale_blocks = stale_after_rename_blocks(meta) if stale_active else []
    if assembled_at:
        # assemble 済み: 中間物は削除済みで、ブロック別出力による進捗数は
        # 未完了に見えてしまう。完成状態を明示して誤認を防ぐ（#20）。
        print(f"[status] work={work.name}: 完成（assemble 済み・中間物削除済み）")
        print(f"[status] assembled_at: {assembled_at}")
        if assembled_outputs:
            shown = ", ".join(assembled_outputs[:8]) + ("…" if len(assembled_outputs) > 8 else "")
            print(f"[status] 出力成果物 ({len(assembled_outputs)}): {shown}")
        if stale_active:
            print(f"[status] 再 assemble が必要（改名により stale）: derivatives_stale_at={stale_at}")
            if stale_blocks:
                print(f"[status]   改名 stale ブロック: {_cap_list(stale_blocks)}")
            print("[status] 次の作業: 再 assemble（改名により stale）")
        else:
            print("[status] 次の作業: なし（完成済み。改訂するには対象ブロックを"
                  " commit --revise で確定し直して再 assemble）")
        return 0

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

    if stale_active:
        print(f"[status] 再 assemble が必要（改名により stale）: derivatives_stale_at={stale_at}")
        if stale_blocks:
            print(f"[status]   改名 stale ブロック: {_cap_list(stale_blocks)}")

    if all_warns:
        print("[status] 警告:")
        for w in all_warns:
            print(f"[status]   {w}")
    else:
        print("[status] 警告: なし")

    # 分析の確認待ち（未確定の声と未解決の候補。P0-4・#7）
    for line in _status_pending_analysis(work):
        print(line)

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


def _merge_aliases(existing, incoming) -> list[str]:
    """aliases の追記マージ（P0-4）。既存の順を保ち、重複（既存・差分内）は足さない。"""
    out: list[str] = list(existing) if isinstance(existing, list) else []
    seen = {v.strip() for v in out if isinstance(v, str) and v.strip()}
    values = incoming if isinstance(incoming, list) else [incoming]
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        if v.strip() in seen:
            continue
        seen.add(v.strip())
        out.append(v.strip())
    return out


def _check_voice_transition(entry: dict, provided: dict, match_value: str) -> None:
    """voice_status の遷移検証（P0-4）。

    confirmed へ遷移するには声の記述（口調・人称・呼び方・語彙・口癖）が必要。
    無ければ ValueError で拒否する（呼び出し側は何も書き込まない）。
    """
    status = provided.get("voice_status")
    if status is not None and status not in VOICE_STATUSES:
        raise ValueError(
            f"{match_value}: voice_status は "
            + " / ".join(VOICE_STATUSES) + " のいずれかであるべきです")
    if status == "confirmed" and not voice_description_present(entry):
        raise ValueError(
            f"{match_value}: voice_status を confirmed にするには声の記述"
            "（speech_style / first_person / second_person / address / vocab / catchphrase）"
            "が必要です")


def _apply_chars_delta(chars: list[dict], delta_list: list[dict]) -> list[str]:
    """キャラ差分を適用。proposed が発生した変更を説明する文字列リストを返す。

    aliases は既存への追記マージ（重複排除）。voice_status = confirmed への
    遷移は声の記述が必要で、無ければ ValueError（P0-4）。
    """
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
            # 声の遷移検証は適用前に行う（拒否時にエントリを変更しない）
            prospective = dict(entry)
            prospective.update({k: v for k, v in updates.items() if k != "aliases"})
            _check_voice_transition(prospective, updates, match_value)
            for k, v in updates.items():
                if k == "aliases":
                    # 追記マージ（既存 aliases を置き換えず、重複を足さない）
                    entry["aliases"] = _merge_aliases(entry.get("aliases"), v)
                    continue
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
            if "aliases" in base:
                base["aliases"] = _merge_aliases([], base["aliases"])
            _check_voice_transition(base, base, match_value)
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


def _block_src_text(work: Path, block_key: str, meta: dict) -> str:
    """block 原文（段落列）を改行で連結して返す。読めなければ空文字。

    辞書の fixed 検査（原文に語が出ているのに規定訳が訳文に無い）に使う。
    """
    b = block_meta(meta, block_key)
    if b is None:
        return ""
    cid = block_key.split("/", 1)[0]
    try:
        paras_data = load_paras(work, cid)
    except (FileNotFoundError, OSError, ValueError):
        return ""
    return "\n".join(p.get("text", "") for p in block_paras(paras_data, b))


def _vocab_ruling_exists(work: Path, finding, cid: str, bid: str,
                         src_hash: str, draft_hash: str, dict_version: str) -> bool:
    """語彙 candidates の裁定記録があるか（rule_id="vocab:<src>"・全キー一致のみ有効）。"""
    from rulings import find_active_ruling
    return find_active_ruling(
        work,
        rule_id=f"vocab:{finding.entry.src}",
        chapter=cid, block=bid, paragraph=finding.paragraph,
        src_hash=src_hash, draft_hash=draft_hash, dict_version=dict_version,
    ) is not None


def _analysis_phase_active(work: Path) -> bool:
    """work が分析工程に入っているかを判定する（分析ゲート・残渣検査の適用条件）。

    通常フローでは lock_prep が分析の前提（flow-contract: analysis requires
    lock_prep）。その成果物（``_work/lock_prep.toml`` / ``lock_prep.md``）がある
    work にだけ、分析ゲートと保存前の残渣検査を適用する。無い work は分析工程に
    入る前の部分 work とみなし、従来どおりの挙動を保つ（後方互換）。
    """
    lp = work / "_work"
    return (lp / "lock_prep.toml").is_file() or (lp / "lock_prep.md").is_file()


def _series_decision(work: Path) -> str:
    """manifest の series_decision（読めなければ空文字）。"""
    try:
        man = load_structured(work / "manifest")
    except Exception:
        return ""
    return str(man.get("series_decision") or "")


def _stop_if_undecided(work: Path, tool: str) -> bool:
    """series_decision が undecided なら分析・翻訳を止める（True=止めた）。

    連作の候補があるのに、引き継ぐか単独かを決めていない状態。決定が記録される
    まで分析も翻訳も始めさせない。
    """
    if _series_decision(work) != "undecided":
        return False
    print(
        f"[{tool}] エラー: series_decision が undecided です（連作の候補があり、決定が未記録）。"
        f"連作として引き継ぐ場合は carry_analysis を実行してから ingest を "
        f"--series-from <前作workdir> で再実行し、単独で進める場合は ingest を "
        f"--standalone 付きで再実行して決定を記録してください",
        file=sys.stderr,
    )
    return True


def _translation_analysis_gate(work: Path, meta: dict, block_key: str,
                               no_analysis: bool, tool: str) -> int:
    """翻訳 prepare / commit の分析ゲート（0=続行可、1=停止）。

    対象 block が「分析済み」（``analysis_committed_at`` または
    ``analysis_no_changes``）でなければ停止する。``--no-analysis`` が明示された
    ときだけ通し、chapters_meta の該当 block に ``analysis_skipped = true`` を
    記録する（assemble の後でも、どのブロックを飛ばしたか追えるように）。
    分析記録の ``analysis_src_hash`` が現行の原文ハッシュと違う場合は警告のみ出す
    （裁定記録の失効は stage11 の範囲）。
    """
    b = block_meta(meta, block_key)
    if b is None:
        return 0  # 未知 block の扱いは各工程の既存のエラー経路に任せる

    analyzed = bool(b.get("analysis_committed_at") or b.get("analysis_no_changes"))
    if not analyzed:
        if not no_analysis:
            print(
                f"[{tool}] エラー: {block_key} は未分析です。先に分析を確定してください"
                f"（prepare --mode analysis → analyze-commit）。"
                f"分析を省いて進める場合だけ --no-analysis を明示してください",
                file=sys.stderr,
            )
            return 1
        _set_block_field(meta, block_key, "analysis_skipped", True)
        save_chapters_meta(work, meta)
        print(
            f"[{tool}] 警告: {block_key} の分析を省略します（--no-analysis）。"
            f"chapters_meta に analysis_skipped = true を記録しました",
            file=sys.stderr,
        )
        return 0

    recorded = str(b.get("analysis_src_hash") or "")
    current = _analysis_src_hash(work, block_key, meta)
    if recorded and current and recorded != current:
        print(
            f"[{tool}] 警告: {block_key} の分析記録は原文の変更で古くなっている可能性があります"
            f"（analysis_src_hash {recorded} != 現行 {current}）。警告のみで続行します",
            file=sys.stderr,
        )
    return 0


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
        # 追加欄の型検証（aliases / voice_status / voice_note。P0-4）
        for key in ("voice_status", "voice_note", "aliases"):
            for loc, holder in (("", c), ("updates", c.get("updates")),
                                ("new_fields", c.get("new_fields"))):
                if not isinstance(holder, dict) or key not in holder:
                    continue
                where = f"characters_delta[{i}]" + (f".{loc}" if loc else "") + f".{key}"
                v = holder[key]
                if key == "voice_status" and (not isinstance(v, str) or v not in VOICE_STATUSES):
                    return False, (f"{where} は "
                                   + " / ".join(VOICE_STATUSES) + " のいずれかであるべきです"), False
                if key == "voice_note" and not isinstance(v, str):
                    return False, f"{where} は文字列であるべきです", False
                if key == "aliases" and (not isinstance(v, list)
                                         or not all(isinstance(x, str) for x in v)):
                    return False, f"{where} は文字列の配列であるべきです", False
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


def cmd_analyze_commit(work: Path, delta_path: Path,
                       force_residue: bool = False) -> int:
    started = time.time()
    rc = _cmd_analyze_commit_impl(work, delta_path, force_residue)
    reason = "success" if rc == 0 else "other"
    _record_stage(work, "analyze-commit", None, None, started, rc, reason=reason)
    return rc


def _cmd_analyze_commit_impl(work: Path, delta_path: Path,
                             force_residue: bool = False) -> int:
    if not delta_path.is_file():
        print(f"[analyze-commit] エラー: delta ファイルが見つかりません: {delta_path}", file=sys.stderr)
        return 1

    ok, lock_reason = _acquire_lock_reason(work, "analyze-commit")
    if not ok:
        print(f"[analyze-commit] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        return 3

    try:
        # 連作の決定が未記録（undecided）のうちは分析を止める
        if _stop_if_undecided(work, "analyze-commit"):
            return 1

        delta = _parse_delta(delta_path)
        block = delta.get("block", "").strip()

        meta = load_chapters_meta(work)

        # 受入検証（_merge_delta の前に実施。拒否時は一切書かない）
        ok, reason, has_changes = validate_analysis_delta(meta, delta)
        if not ok:
            print(f"[analyze-commit] エラー: 拒否しました: {reason}", file=sys.stderr)
            return 1

        # 反映後の仮の全体を組み立てる（書き込みはまだ行わない）。
        # 分析工程に入った work では、対象言語の欄（*_src 以外）に中国語が残って
        # いないかを保存の前に検査する（P0-4 / #8）。行ではなく TOML の構造で
        # 対象の欄を選ぶ（find_cn_residue_fields）。
        check_active = _analysis_phase_active(work)
        payloads: list[tuple[Path, dict]] | None = None
        logs: list[str] = []
        candidate_logs: list[str] = []
        candidates_changed = False
        if has_changes or check_active:
            payloads, logs, errors = _build_analysis_payloads(work, delta)
            if payloads is None:
                for e in errors:
                    print(f"[analyze-commit] エラー: {e}", file=sys.stderr)
                print("[analyze-commit] エラー: マージ検証失敗、何も書きませんでした", file=sys.stderr)
                return 1
            # 確認の候補（話者・異表記）: 差分適用後のデータと対象ブロックの
            # 原文から抽出して記録する（確定はしない。P0-4・#7）
            candidate_logs, candidates_changed = _record_voice_candidates(
                work, block, meta, payloads)

        forced_residue = False
        if check_active and payloads is not None:
            from detect.zh2ja import find_cn_residue_fields
            residue: list[tuple[str, object]] = []
            for r_path, r_data in payloads:
                for f in find_cn_residue_fields(r_data):
                    residue.append((r_path.name, f))
            if residue:
                for r_name, f in residue[:20]:
                    print(f"[analyze-commit] 残渣: {r_name}: {f.value} ({f.context})")
                if len(residue) > 20:
                    print(f"[analyze-commit] … 他 {len(residue) - 20} 件")
                if not force_residue:
                    print(
                        f"[analyze-commit] エラー: 分析 TOML の対象欄に中国語の残渣が "
                        f"{len(residue)} 件あります。修正するか --force-residue を明示してください"
                        f"（保存しません）",
                        file=sys.stderr,
                    )
                    return 1
                forced_residue = True
                print(
                    f"[analyze-commit] 警告: --force-residue により残渣 {len(residue)} 件を"
                    f"無視して保存します",
                    file=sys.stderr,
                )

        if has_changes or candidates_changed:
            write_errors = _commit_analysis_payloads(payloads, work)
            if write_errors:
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

        if forced_residue:
            # 残渣を承知で通したことを block の記録に残す
            _set_block_field(meta, block, "analysis_forced_residue", True)

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
        for l in candidate_logs:
            print(f"[analyze-commit] {l}")
        return 0
    finally:
        _release_lock(work)


# ---------------------------------------------------------------------------
# 確認の候補（analyze-commit。話者・異表記の抽出。P0-4・#7）
# ---------------------------------------------------------------------------

# 話者候補: 名前が引用符の近くに出る回数の窓と閾値（実装で決定・テストで固定）
SPEAKER_QUOTE_WINDOW = 20      # 引用符からこの字数以内を「近く」とする
SPEAKER_MIN_NEAR_QUOTES = 2    # 引用符の近くに出る回数がこれ以上で候補
# 引用符・括りの集合（原文側。「」『』“”‘’" ' ［］[]。align_check の QUOTE_HEADS に準拠）
_QUOTE_MARKS = frozenset("「」『』“”‘’\"'［］[]")
# 異表記候補: 名前の最短長と、原文側だけに出る揺れ表記の採用閾値（実装で決定）
VARIANT_MIN_NAME_LEN = 2
VARIANT_TEXT_MIN_OCCURRENCES = 2


def _name_forms(entry: dict) -> list[str]:
    """キャラの名前表記（name_src / name_ja / ruby / aliases）を列挙（空・重複除去）。"""
    forms: list[str] = []
    for key in ("name_src", "name_ja", "ruby"):
        v = str(entry.get(key) or "").strip()
        if v and v not in forms:
            forms.append(v)
    for a in character_aliases(entry):
        if a not in forms:
            forms.append(a)
    return forms


def _similar_names(a: str, b: str) -> bool:
    """近い表記の名前か（異表記候補の判定。実装で決定・テストで固定）。

    - 大小文字違いだけ: 同じ扱い
    - 同じ長さ: 1文字だけ違う（繁体/簡体・誤記・表記ゆれの候補）
    - 長さ違い: 短い方が長い方に含まれ、長さの差が1以内（付加された別表記）
    """
    a, b = a.strip(), b.strip()
    if not a or not b or a == b:
        return False
    if len(a) < VARIANT_MIN_NAME_LEN or len(b) < VARIANT_MIN_NAME_LEN:
        return False
    if a.casefold() == b.casefold():
        return True
    if len(a) == len(b):
        return sum(1 for x, y in zip(a, b) if x != y) <= 1
    short, longer = (a, b) if len(a) < len(b) else (b, a)
    return len(longer) - len(short) <= 1 and short in longer


def _near_quote_hits(text: str, name: str) -> int:
    """text 中の name の出現のうち、引用符の近く（窓内）にある回数。"""
    hits = 0
    start = 0
    while True:
        i = text.find(name, start)
        if i < 0:
            break
        lo = max(0, i - SPEAKER_QUOTE_WINDOW)
        hi = min(len(text), i + len(name) + SPEAKER_QUOTE_WINDOW)
        if any(ch in _QUOTE_MARKS for ch in text[lo:hi]):
            hits += 1
        start = i + len(name)
    return hits


def _record_voice_candidates(work: Path, block_key: str, meta: dict,
                             payloads: list[tuple[Path, dict]]) -> tuple[list[str], bool]:
    """差分適用後に対象ブロックの原文を走査し、確認の候補を記録する（P0-4・#7）。

    - 話者の確認候補: 名前が引用符の近くに閾値回数以上出る人物。
      「名前が近くにあるだけでは話者と確定できない」のでセリフありとは扱わず、
      `speaker_candidate`（ブロックキーのリスト）として記録するだけにする。
    - 異表記候補: 近い表記の名前が原文で併存しているもの。`variant_candidates`
      として記録し、aliases に入るまで残す（解決済みは自動で落とす）。該当
      キャラが無い候補は work レベル（characters TOML トップ）に置く。

    確定はしない（推測で埋めない）。戻り値: (表示ログ, データを変更したか)。
    """
    chars_data = next((d for p, d in payloads if p.name.startswith("characters")), None)
    if not isinstance(chars_data, dict):
        return [], False
    text = _block_src_text(work, block_key, meta)
    chars = chars_data.get("characters")
    if not isinstance(chars, list) or not text.strip():
        return [], False

    log: list[str] = []
    changed = False
    entries = [(c, _name_forms(c)) for c in chars if isinstance(c, dict)]
    known: set[str] = {f for _c, forms in entries for f in forms}
    owners: dict[str, dict] = {}
    for c, forms in entries:
        for f in forms:
            owners.setdefault(f, c)

    # 解決済みの異表記候補を先に落とす（aliases へ移った・相手の別名になった）
    for c, _forms in entries:
        raw = c.get("variant_candidates")
        kept = []
        for cand in variant_candidates_of(c):
            other, own = cand["name_src"], cand["similar_to_src"]
            if other in character_aliases(c):
                continue
            owner = owners.get(other)
            if owner is not None and own and own in character_aliases(owner):
                continue
            kept.append(cand)
        if isinstance(raw, list) and len(kept) != len(variant_candidates_of(c)):
            if kept:
                c["variant_candidates"] = kept
            else:
                c.pop("variant_candidates", None)
            changed = True

    # ① 話者の確認候補（名前が引用符の近くに出る。セリフありとは扱わない）
    for c, forms in entries:
        name = c.get("name_ja") or c.get("name_src") or "(無名)"
        if voice_status_of(c) == "confirmed":
            # 声が確定した人物の候補は役目を終える
            if speaker_candidate_blocks(c):
                c.pop("speaker_candidate", None)
                changed = True
            continue
        hits = sum(_near_quote_hits(text, f) for f in forms)
        if hits < SPEAKER_MIN_NEAR_QUOTES:
            continue
        existing = speaker_candidate_blocks(c)
        merged = merge_speaker_candidate(existing, [block_key])
        if merged != existing:
            c["speaker_candidate"] = merged
            changed = True
            log.append(
                f"話者の確認候補: {name} ({block_key})（引用符の近くに"
                f"{SPEAKER_MIN_NEAR_QUOTES}回以上。名前があるだけで話者とは確定しない）")

    # ② 異表記候補（キャラ同士。近い表記が原文に併存しているもの）
    for i, (ca, forms_a) in enumerate(entries):
        for cb, forms_b in entries[i + 1:]:
            for a in forms_a:
                if a not in text:
                    continue
                for b in forms_b:
                    if b not in text or not _similar_names(a, b):
                        continue
                    if b in character_aliases(ca) or a in character_aliases(cb):
                        continue  # 既に aliases で結ばれている＝解決済み
                    ea = variant_candidates_of(ca)
                    eb = variant_candidates_of(cb)
                    ma = merge_variant_candidates(
                        ea, [{"name_src": b, "similar_to_src": a, "block": block_key}])
                    mb = merge_variant_candidates(
                        eb, [{"name_src": a, "similar_to_src": b, "block": block_key}])
                    if ma != ea:
                        ca["variant_candidates"] = ma
                        changed = True
                    if mb != eb:
                        cb["variant_candidates"] = mb
                        changed = True
                    if ma != ea or mb != eb:
                        log.append(
                            f"異表記候補: {'⇔'.join(sorted((a, b)))} ({block_key})"
                            f"（同一人物の異表記の可能性。aliases に入れるか別人と記録するまで残す）")

    # ③ 異表記候補（原文側だけに近い表記がある。該当キャラが無い → work レベル）
    raw_work = chars_data.get("variant_candidates")
    work_cands = normalize_variant_candidates(raw_work)
    kept_work = [x for x in work_cands if x["name_src"] not in known]
    if isinstance(raw_work, list) and len(kept_work) != len(work_cands):
        changed = True
    work_cands = kept_work
    for _c, forms in entries:
        for n in forms:
            if len(n) < VARIANT_MIN_NAME_LEN or n not in text:
                continue
            length = len(n)
            seen_t: set[str] = set()
            for i in range(0, len(text) - length + 1):
                t = text[i:i + length]
                if t == n or t in seen_t or t in known or not t.isalnum():
                    continue
                if not _similar_names(n, t):
                    continue
                seen_t.add(t)
                if text.count(t) < VARIANT_TEXT_MIN_OCCURRENCES:
                    continue
                merged = merge_variant_candidates(
                    work_cands, [{"name_src": t, "similar_to_src": n, "block": block_key}])
                if merged != work_cands:
                    work_cands = merged
                    changed = True
                    log.append(
                        f"異表記候補: {'⇔'.join(sorted((t, n)))} ({block_key})"
                        f"（原文に近い表記が併存。該当キャラなし。"
                        f"aliases に入れるか別人と記録するまで残す）")
    if work_cands:
        if chars_data.get("variant_candidates") != work_cands:
            chars_data["variant_candidates"] = work_cands
            changed = True
    elif isinstance(raw_work, list) and raw_work:
        chars_data.pop("variant_candidates", None)
        changed = True
    return log, changed


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
    if isinstance(residue, dict):
        vocab = residue.get("vocab", {})
        vocab_stops = int(vocab.get("stops", 0) or 0) if isinstance(vocab, dict) else 0
        if (int(residue.get("errors", 0) or 0) > 0
                or int(residue.get("candidates", 0) or 0) > 0
                or vocab_stops > 0):
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


def _find_first_unanalyzed(chapters: list[dict]) -> tuple[str, str, dict] | None:
    """最初の未分析ブロックを返す（#4b）。

    「未分析」= ``analysis_committed_at`` も ``analysis_no_changes`` も無い block。
    分析モードの prepare は翻訳の未完了ではなく、この基準で選択する。
    """
    for ch in chapters:
        cid = ch["id"]
        for b in ch.get("blocks", []):
            if not (b.get("analysis_committed_at") or b.get("analysis_no_changes")):
                return cid, b["id"], b
    return None


def cmd_prepare(work: Path, chapter: str | None, block: str | None,
                mode: str = "translation", no_analysis: bool = False) -> int:
    started = time.time()
    outcome: dict = {"cid": chapter, "bid": block}
    rc = _cmd_prepare_impl(work, chapter, block, mode, outcome, no_analysis)
    _record_stage(work, "prepare", outcome["cid"], outcome["bid"], started, rc, mode=mode)
    return rc


def _cmd_prepare_impl(work: Path, chapter: str | None, block: str | None,
                      mode: str, outcome: dict, no_analysis: bool = False) -> int:
    if not work.is_dir():
        print(f"[prepare] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 排他ロック（共有ロックに一元化。解放は finally で必ず行う）
    ok, lock_reason = _acquire_lock_reason(work, "prepare")
    if not ok:
        print(f"[prepare] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        return 3
    try:
        return _cmd_prepare_locked(work, chapter, block, mode, outcome, no_analysis)
    finally:
        _release_lock(work)


def _cmd_prepare_locked(work: Path, chapter: str | None, block: str | None,
                        mode: str, outcome: dict, no_analysis: bool = False) -> int:
    # スキル版ドリフト警告: 記録された skill_version と実行中の版が違えば知らせる
    # （非致命。記録が無い旧作業ディレクトリでは警告しない）。
    try:
        man = load_structured(work / "manifest")
    except (FileNotFoundError, OSError):
        man = {}

    # assemble 済み（完成・中間物削除済み）の work は、未確定ブロックが残って
    # いるように見えても黙って続行しない。案内を出して非ゼロで停止する（#20）。
    assembled_at = str(man.get("assembled_at") or "")
    if assembled_at:
        print(f"[prepare] エラー: この work は assemble 済みです（assembled_at: {assembled_at}）。"
              f"完成・中間物削除済みのため、未完了として続行しません", file=sys.stderr)
        print("[prepare] 改訂するには、対象ブロックのドラフトを output/_work/ に置いて"
              " commit --revise で確定し直し、再 assemble してください", file=sys.stderr)
        return 1

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

    # 連作の決定が未記録（undecided）のうちは、分析も翻訳も始めさせない
    if _stop_if_undecided(work, "prepare"):
        return 1

    src, dst = direction(work)
    suffix = target_suffix(dst)

    if mode == "analysis":
        # 分析モードは「未分析の先頭ブロック」を選ぶ（翻訳の未完了ではない: #4b）
        if chapter and block:
            cid, bid = chapter, block
        elif chapter:
            cid = chapter
            ch = next((c for c in chapters if c["id"] == cid), None)
            if ch is None:
                print(f"[prepare] エラー: 章 {cid} が見つかりません", file=sys.stderr)
                return 1
            result = _find_first_unanalyzed([ch])
            if result is None:
                print(f"[prepare] 章 {cid} は全ブロック分析済みです", file=sys.stderr)
                return 1
            cid, bid, _ = result
        else:
            result = _find_first_unanalyzed(chapters)
            if result is None:
                print("[prepare] 分析は完了しています（全ブロックが分析済みです）", file=sys.stderr)
                return 1
            cid, bid, _ = result
    else:
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

    # 分析ゲート: 分析工程に入った work では、未分析ブロックの翻訳は
    # --no-analysis の明示があるときだけ通す（prepare と commit の両方で確認する）
    if mode != "analysis" and _analysis_phase_active(work):
        rc_gate = _translation_analysis_gate(work, meta, f"{cid}/{bid}", no_analysis, "prepare")
        if rc_gate:
            return rc_gate

    # コンテキスト生成
    work_dir_obj = work
    (work_dir_obj / "translation_input").mkdir(parents=True, exist_ok=True)
    from build_context import _stage_impl as bc_stage
    bc_stage(work, cid, bid, mode)

    ctx_path = work / "translation_input" / f"{cid}_{bid}_{mode}.md"

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
               model: str, prompt_version: str, revise: bool = False,
               no_analysis: bool = False) -> int:
    """commit を実行し、成功/失敗いずれでも timeline.jsonl に1行記録する。"""
    started = time.time()
    outcome: dict = {"reason": "other", "commit_id": None, "snap_dir": None}
    rc = _cmd_commit_impl(work, chapter, block, model, prompt_version, revise, outcome,
                          no_analysis)
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
                     outcome: dict, no_analysis: bool = False) -> int:
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
        # 連作の決定が未記録（undecided）のうちは確定させない
        if _stop_if_undecided(work, "commit"):
            outcome["reason"] = "other"
            return 1

        # 分析ゲート（確定の直前の確認）: 分析工程に入った work では、
        # 未分析ブロックの確定は --no-analysis の明示があるときだけ通す
        try:
            gate_meta = load_chapters_meta(work)
        except Exception as e:
            print(f"[commit] エラー: {e}", file=sys.stderr)
            outcome["reason"] = "exception"
            return 1
        if _analysis_phase_active(work):
            rc_gate = _translation_analysis_gate(
                work, gate_meta, f"{chapter}/{block}", no_analysis, "commit")
            if rc_gate:
                outcome["reason"] = "other"
                return rc_gate

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
            from vocab import check_draft as vocab_check_draft, house_style_paths, load_vocab

            # house_style の known_terms を読み込む（探索先は vocab.house_style_paths に
            # 共通化。#19: 従来は prepare と commit で探索先が不一致だった）
            known_terms: set[str] = set()
            for house_candidate in house_style_paths(work):
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

            # 統合辞書（作品 terms / house_style / 拡張 vocab.toml）による検査（P0-3 / #6）。
            # fixed: 違反は停止。candidates: 残存は停止（裁定記録 `vocab:<src>` で通過。
            # 全キー一致のみ有効）。ja_ok は check_draft が除外する。
            vocab = load_vocab(work)
            v_findings = vocab_check_draft(
                vocab, items, _block_src_text(work, f"{cid}/{bid}", meta),
                known_terms=known_terms)
            v_src_hash = _analysis_src_hash(work, f"{cid}/{bid}", meta)
            v_ruled = 0
            v_stops = []
            for vf in v_findings:
                if vf.kind == "candidates" and _vocab_ruling_exists(
                        work, vf, cid, bid, v_src_hash, draft_hash,
                        vocab.dict_version()):
                    v_ruled += 1
                    where = f"P{vf.paragraph:03d}" if vf.paragraph else "ブロック全体"
                    print(f"[commit] 残渣(candidates): 「{vf.form}」は裁定記録により通過 ({where})")
                    continue
                v_stops.append(vf)
            report["stages"]["residue"]["vocab"] = {
                "fixed": sum(1 for f in v_findings if f.kind == "fixed"),
                "candidates": sum(1 for f in v_findings if f.kind == "candidates"),
                "ruled": v_ruled,
                "stops": len(v_stops),
            }
            if v_stops:
                for vf in v_stops:
                    where = f"P{vf.paragraph:03d}" if vf.paragraph else "ブロック全体"
                    print(f"[commit] 残渣({vf.kind}): {vf.detail} ({where})")
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。辞書の残渣 {len(v_stops)} 件。fixed は本文修正、"
                      f"candidates は本文修正か裁定記録（rulings.py。dict_version="
                      f"{vocab.dict_version()}）で通せます。", file=sys.stderr)
                out["reason"] = "residue"
                return 1
            if v_ruled:
                print(f"[commit] 残渣(辞書): {v_ruled} 件は裁定記録により通過")

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

# review 出力の冒頭に固定で入れる限界の明示（文言固定・改変禁止）
REVIEW_LIMITATION = (
    "この抜き取りは補助です。意味・否定・話者・固有名・声・自然さは、"
    "機械の PASS では保証されません。必ず目視で確認してください。"
)


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


def _list_audited_blocks(work: Path) -> list[tuple[str, str]]:
    """assemble 後の work で、残した監査記録から確定ブロックを列挙する（#20）。

    ブロック別出力は中間物として削除済みのため、output/_work/commit/ の
    report.json（commit の報告）を使う。監査記録を残す前の assemble で
    作られた work では、chapters_meta の emit 済み記録で代用する。
    """
    found: set[tuple[str, str]] = set()
    commit_dir = work / "output" / "_work" / "commit"
    if commit_dir.is_dir():
        for entry in commit_dir.iterdir():
            rp = entry / "report.json"
            if not entry.is_dir() or not rp.is_file():
                continue
            try:
                data = json.loads(rp.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            cid = str(data.get("chapter") or "")
            bid = str(data.get("block") or "")
            if cid and bid:
                found.add((cid, bid))

    try:
        meta = load_chapters_meta(work)
    except Exception:
        meta = None
    if not found and meta is not None:
        for ch in meta.get("chapters", []):
            for b in ch.get("blocks", []):
                if b.get("status") == "emitted" or b.get("emitted_at"):
                    found.add((str(ch.get("id") or ""), str(b.get("id") or "")))

    if meta is not None:
        ordered: list[tuple[str, str]] = []
        for ch in meta.get("chapters", []):
            cid = str(ch.get("id") or "")
            for b in ch.get("blocks", []):
                key = (cid, str(b.get("id") or ""))
                if key in found:
                    ordered.append(key)
        if ordered:
            return ordered
    return sorted(found)


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


def _review_seed(work_id: str, wave: int) -> str:
    """抜き取りの固定 seed（work + wave の sha256 先頭16桁）。

    Python の hash() はプロセスごとに変わる（PYTHONHASHSEED）ため使わない。
    同じ work・同じ wave ならプロセスが違っても同じ箇所を選ぶ。
    """
    return hashlib.sha256(f"{work_id}:{wave}".encode("utf-8")).hexdigest()[:16]


def _sample_paragraphs(work: Path, blocks: list[tuple[str, str]],
                        ratio: float, seed: str) -> list[dict]:
    """指定ブロックの段落から ratio の割合で決定的に選択する（seed は固定）。"""
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


def _canonical_translation_index(work: Path) -> dict[tuple[str, str, int], str]:
    """実訳の索引: text_scope の正本を (章, ブロック, 段落番号) で引けるようにする。

    正本は text_scope.block_texts（done → 確定ブロック出力の順）。段落対応の
    データ（段落番号）から実訳を引くので、[Pnnn] の文字列探索に依存しない。
    段落対応が壊れたブロックがあっても全体を落とさず、読める範囲だけを
    索引する（読めない段落は呼び出し側で「不明」と出す）。
    """
    import text_scope as ts
    try:
        entries = ts.block_texts(work)
    except ts.TextScopeError:
        entries = _canonical_translation_entries_degraded(work)
    return {(e.chapter, e.block, e.para): e.text for e in entries}


def _canonical_translation_entries_degraded(work: Path) -> list:
    """読める範囲の正本だけをブロック単位で集める（旧形式対応）。

    text_scope.block_texts は一括で読むため、1 ブロックの段落数不一致などで
    全体が読めなくなる。review は「現行の確定出力から読める範囲で動く」ため、
    text_scope のブロック単位の読取関数を再利用し、壊れたブロックだけを飛ばす。
    """
    import text_scope as ts
    try:
        meta = load_chapters_meta(work)
    except FileNotFoundError:
        return []
    suf = target_suffix(direction(work)[1])
    entries: list = []
    for ch in meta.get("chapters", []):
        cid = ch.get("id")
        if not isinstance(cid, str):
            continue
        for blk in ch.get("blocks", []):
            if not isinstance(blk, dict) or not isinstance(blk.get("id"), str):
                continue
            bid = blk["id"]
            done_p = ts._done_path(work, cid, bid, suf)
            if done_p.is_file():
                entries.extend(ts._block_from_numbered(cid, bid, done_p, "done"))
                continue
            emit_p = ts._emitted_path(work, cid, bid, suf)
            if emit_p.is_file():
                try:
                    entries.extend(ts._block_from_emitted(cid, bid, emit_p, blk))
                except ts.TextScopeError:
                    continue
    return entries


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

    # 確定ブロック。assemble 後はブロック別出力が purge 済みのため、
    # 残した監査記録（commit の report.json）から復元して読む（#20）。
    blocks = _list_committed_blocks(work)
    assembled_at, _assembled_outputs = _assembled_state(work)
    from_audit = False
    if not blocks and assembled_at:
        blocks = _list_audited_blocks(work)
        from_audit = bool(blocks)
    if not blocks:
        print(f"[review] エラー: 確定済みブロックがありません", file=sys.stderr)
        return 1

    # 改名 stale（再検収が必要なブロック）。status / review は stale を
    # 「最新・検収済み」と扱わない（P0-6・#22）。
    try:
        _meta = load_chapters_meta(work)
    except Exception:
        _meta = None
    stale_blocks = stale_after_rename_blocks(_meta) if _meta else []

    # 1) 機械の指摘
    reports = _scrape_commit_reports(work, blocks)
    findings = _extract_machine_findings(reports)
    machine_items = findings

    # 2) リスク箇所
    risk_items = _find_risk_blocks(work, blocks)

    # 3) 抜き取り（work + wave の sha256 由来の固定 seed。プロセス非依存）
    seed = _review_seed(work.name, wave)
    sample_items = _sample_paragraphs(work, blocks, sample_ratio, seed)

    # 実訳は text_scope の正本（done → 確定ブロック出力）から段落番号で引く。
    # [Pnnn] の文字列探索はしない（通常の emit はマーカーを消すため）。
    trans_index = _canonical_translation_index(work)

    # 出力
    src, dst = direction(work)
    rev_dir = work / "_work"
    rev_dir.mkdir(parents=True, exist_ok=True)
    rev_path = rev_dir / f"review_wave{wave}.md"

    # 検収リストを書く（冒頭に限界の明示。文言固定）
    rev_lines = [
        REVIEW_LIMITATION,
        "",
        f"# 検収リスト wave {wave}",
        "",
        f"work: {work.name}",
        f"対象ブロック数: {len(blocks)}",
        f"seed: {seed}",
        f"生成: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
    ]

    # 改名 stale（再 assemble と再検収が必要。検収済みとして扱わない）
    rev_lines.append(f"## 0. 改名により stale（再検収が必要）（{len(stale_blocks)}件）")
    if stale_blocks:
        for b in stale_blocks:
            rev_lines.append(f"- {b}: 改名後に再検収が必要（再 assemble → 該当ブロックを再確認）")
    else:
        rev_lines.append("（なし）")
    rev_lines.append("")

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

    # 抜き取り — 原文と訳文（正本から段落番号で引く）を並べる
    rev_lines.append(f"## 3. 抜き取り（{len(sample_items)}段落）")
    if sample_items:
        rev_lines.append(
            "選定: " + ", ".join(f"{s['block']} P{s['para']}" for s in sample_items))
        rev_lines.append("")
        for s in sample_items:
            blk = s["block"]
            pn = s["para"]
            rev_lines.append(f"### {blk} P{pn}")
            rev_lines.append("")
            rev_lines.append(f"原文: {s['text']}")
            cid, bid = blk.split("/")
            dst_text = trans_index.get((cid, bid, pn))
            if dst_text is None:
                rev_lines.append("訳文: 不明")
            else:
                rev_lines.append(f"訳文: {dst_text}")
            rev_lines.append("")
    else:
        rev_lines.append("（なし）")
    rev_lines.append("")

    rev_lines.append("---")
    rev_lines.append("機械指摘は commit 時の report.json から抽出。リスク箇所は characters.toml の arcs/address/ruby から。")
    rev_lines.append(
        f"抜き取りは固定 seed（work + wave の sha256 先頭16桁 = {seed}）で決定的。"
        "実訳は text_scope の正本（done → 確定ブロック出力）から段落番号で引く。")
    rev_lines.append("")

    rev_path.write_text("\n".join(rev_lines), encoding="utf-8")

    if json_output:
        import json as _json
        out = {
            "wave": wave,
            "total_blocks": len(blocks),
            "seed": seed,
            "limitation": REVIEW_LIMITATION,
            "stale_blocks": stale_blocks,
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

    print(f"[review] {REVIEW_LIMITATION}")
    print(f"[review] wave {wave}: 対象 {len(blocks)} ブロック")
    if stale_blocks:
        print(f"[review] 改名により stale（要再検収）: {len(stale_blocks)}件 ({_cap_list(stale_blocks)})")
    print(f"[review] 機械指摘: {len(machine_items)}件 ({_fmt(machine_items)})")
    print(f"[review] リスク箇所: {len(risk_items)}件 ({_fmt(risk_items)})")
    print(f"[review] 抜き取り: {len(sample_items)}段落 ({_fmt(sample_items)})")
    print(f"[review] seed: {seed}（work + wave の sha256 先頭16桁）")
    print(f"[review] 検収対象ファイル: {rev_path}")
    if assembled_at:
        print(f"[review] assemble 済み（assembled_at={assembled_at}）")
        if from_audit:
            print(f"[review] ブロック別出力は削除済み。残した監査記録 {len(reports)} 件"
                  f"（output/_work/commit/）から読み取りました")

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
    p_ac.add_argument("--force-residue", action="store_true",
                      help="分析 TOML の中国語残渣を警告のうえで通す（block 記録に残す）")

    # prepare
    p_prep = sub.add_parser("prepare", help="翻訳コンテキスト準備")
    p_prep.add_argument("work", help="作品ディレクトリ")
    p_prep.add_argument("--chapter", default=None, help="章（例: ch001）")
    p_prep.add_argument("--block", default=None, help="ブロック（例: b2）")
    p_prep.add_argument("--mode", choices=["translation", "analysis"], default="translation",
                        help="コンテキストのモード（既定: translation）")
    p_prep.add_argument("--no-analysis", action="store_true",
                        help="未分析ブロックの翻訳を明示的に許可する（analysis_skipped として記録）")

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
    p_com.add_argument("--no-analysis", action="store_true",
                       help="未分析ブロックの確定を明示的に許可する（analysis_skipped として記録）")

    args = ap.parse_args()

    work = Path(args.work).resolve()

    if args.command == "status":
        raise SystemExit(cmd_status(work))
    elif args.command == "analyze-commit":
        raise SystemExit(cmd_analyze_commit(work, Path(args.delta),
                                            force_residue=args.force_residue))
    elif args.command == "prepare":
        raise SystemExit(cmd_prepare(work, args.chapter, args.block, mode=args.mode,
                                     no_analysis=args.no_analysis))
    elif args.command == "review":
        raise SystemExit(cmd_review(work, wave=args.wave,
                                     sample_ratio=args.sample_ratio,
                                     json_output=args.json))
    elif args.command == "commit":
        raise SystemExit(cmd_commit(work, args.chapter, args.block,
                                     args.model, args.prompt_version,
                                     revise=args.revise,
                                     no_analysis=args.no_analysis))


if __name__ == "__main__":
    main()