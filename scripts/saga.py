"""saga-translator CLI エントリポイント。ステータス確認と分析コミット。"""
from __future__ import annotations

import argparse
import copy
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
    VOICE_STATUSES, acquire_lock, append_jsonl, atomic_write_text, block_meta, block_paras,
    block_src_hash, character_aliases, derivatives_stale_at, direction, dump_toml,
    load_chapters_meta,
    load_characters, load_paras,
    load_plot, load_structured, load_terms, load_toml, merge_speaker_candidate,
    merge_variant_candidates, normalize_variant_candidates, read_skill_version, record_timeline,
    release_lock, save_chapters_meta, save_toml, sha12, speaker_candidate_blocks,
    stale_after_rename_blocks, target_suffix,
    timed_stage, utf8, variant_candidates_of, voice_description_present, voice_status_of,
)
from check_src_keys import resolve_delta_path  # noqa: E402  # --delta の work 相対解決（G43）

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


def _naming_policy_of(work: Path) -> dict | None:
    """house_style.toml（共通探索先）の [naming_policy] を読む（無ければ None）。

    命名方針の未決定警告（P0-5）に使う。探索先は #19 の house_style_paths
    （work/_house → work.parent/_house）。
    """
    from vocab import house_style_paths

    for p in house_style_paths(work):
        data = _load_toml_safe(p)
        if not isinstance(data, dict):
            continue
        pol = data.get("naming_policy")
        if isinstance(pol, dict) and (str(pol.get("style") or "").strip()
                                      or str(pol.get("codename") or "").strip()):
            return pol
    return None


def _status_pending_analysis(work: Path) -> list[str]:
    """status の「分析の確認待ち」節（P0-4・#7 / P2-9）。

    未確定の声（unconfirmed / insufficient_evidence）と、未解決の話者/異表記の
    確認候補、proposed が残っているエントリ（訳語変更の候補）を列挙する。
    検収済みの「未確定」は許容する（推測で埋めない方針の明示）。
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
    proposed_wait: list[str] = []
    seen_pairs: set[tuple[str, str]] = set()
    # 名前の形 → キャラ（別人として棄却済みの候補を出さないための照合用。5b）
    owners_by_form: dict[str, dict] = {}
    for c in chars:
        for f in _name_forms(c):
            owners_by_form.setdefault(f, c)

    def _proposed_line(name: str, proposed: str) -> str:
        """proposed 滞留の1行（名前＋先頭40字。P2-9）。"""
        head = proposed.splitlines()[0] if proposed else ""
        return f"{name}: {head[:40]}"

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
            if _candidate_dismissed(cand, c, owners_by_form):
                continue  # 別人として棄却済み（dismiss_variant）
            pair = tuple(sorted((cand["name_src"], cand["similar_to_src"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            variant_wait.append(_variant_line(cand["name_src"], cand["similar_to_src"], cand["block"]))
        proposed = str(c.get("proposed") or "").strip()
        if proposed:
            proposed_wait.append(_proposed_line(str(name), proposed))
    for cand in normalize_variant_candidates(chars_data.get("variant_candidates")):
        if _work_candidate_dismissed(cand, owners_by_form):
            continue  # 別人として棄却済み（dismiss_variant）
        pair = tuple(sorted((cand["name_src"], cand["similar_to_src"])))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        variant_wait.append(
            _variant_line(cand["name_src"], cand["similar_to_src"], cand["block"], "（該当キャラなし）"))

    # proposed の滞留（用語集。P2-9）
    try:
        terms_list = load_terms(work).get("terms") or []
    except Exception:
        terms_list = []
    for t in terms_list:
        if not isinstance(t, dict):
            continue
        proposed = str(t.get("proposed") or "").strip()
        if proposed:
            tname = str(t.get("term_ja") or t.get("term_src") or "(無名)")
            proposed_wait.append(_proposed_line(tname, proposed))

    if not voice_wait and not speaker_wait and not variant_wait and not proposed_wait:
        return ["[status] 分析の確認待ち: なし"]
    out = ["[status] 分析の確認待ち:"]
    if voice_wait:
        out.append("[status]   声の状態(未確認/根拠不足): " + _cap_list(voice_wait))
    if speaker_wait:
        out.append("[status]   話者の確認候補: " + _cap_list(speaker_wait))
    if variant_wait:
        out.append("[status]   異表記候補: " + _cap_list(variant_wait))
    if proposed_wait:
        out.append("[status]   proposed 滞留（訳語変更の候補。承認後に rename_terms で本採用）:")
        for x in proposed_wait[:8]:
            out.append(f"[status]     {x}")
        if len(proposed_wait) > 8:
            out.append(f"[status]     …他{len(proposed_wait) - 8}件")
    out.append("[status]   （検収済みの「未確定」は許容。推測で埋めず、埋め直しは1周後に delta で1回）")
    return out


# ---------------------------------------------------------------------------
# status の再開票（P2-4）
# ---------------------------------------------------------------------------
#
# 圧縮の後でも、status の出力だけで再開できるようにする（圧縮で知識が残って
# いる前提にしない）。工程・対象ブロック・分析と辞書の版・未裁定・draft-append の
# 受領済み範囲を毎回読み直して出す。対象ブロックのコンテキストは毎回読む前提で、
# ここで減らすのは同じ版の重複した読み込みだけ（レビュー §3 P2-4）。

# age_policy の検出記録（未裁定の件数を数える。detect/zh2ja.py と同名）
AGE_POLICY_REPORT_TOML = "age_policy_report-[ネタバレあり].toml"


def _count_unsettled_age_policy(work: Path) -> int:
    """age_policy_report の未裁定（ruling が空）件数。記録が無ければ 0。"""
    data = _load_toml_safe(work / "analysis" / AGE_POLICY_REPORT_TOML)
    if not isinstance(data, dict):
        return 0
    n = 0
    for m in data.get("mentions") or []:
        if isinstance(m, dict) and not str(m.get("ruling") or "").strip():
            n += 1
    return n


def _count_unacked_confirms(work: Path, cid: str, bid: str) -> str:
    """対象ブロックの align confirm（未裁定）件数。検査できない場合は「—」。"""
    try:
        from align import check_block
        findings = check_block(work, cid, bid)
    except Exception:
        return "—"
    return str(sum(1 for f in findings if f.severity == "confirm"))


def _count_pending_vocab_candidates(work: Path, meta: dict, cid: str, bid: str,
                                    draft_p: Path) -> str:
    """対象ブロックの語彙 candidates（裁定未了で確定が止まる件数）。検査できない場合は「—」。

    確定（commit）と同じ検査をドラフトに対して行い、裁定記録で通過しないものだけを
    数える（辞書版は統合辞書の dict_version。裁定の全キー一致に使う値と同じ）。
    """
    try:
        from align import parse_numbered
        from vocab import check_draft as vocab_check_draft, house_style_paths, load_vocab
        items = parse_numbered(draft_p.read_text(encoding="utf-8"))
        vocab = load_vocab(work)
        known_terms: set[str] = set()
        for hp in house_style_paths(work):
            data = _load_toml_safe(hp)
            if isinstance(data, dict) and isinstance(data.get("residue"), dict):
                kt = data["residue"].get("known_terms")
                if isinstance(kt, list):
                    known_terms.update(str(x) for x in kt)
        key = f"{cid}/{bid}"
        findings = vocab_check_draft(vocab, items, _block_src_text(work, key, meta),
                                     known_terms=known_terms,
                                     src_items=_block_src_items(work, key, meta))
        draft_hash = sha12(draft_p.read_bytes())
        src_hash = _analysis_src_hash(work, key, meta)
        dict_version = vocab.dict_version()
        n = 0
        for vf in findings:
            if vf.kind != "candidates":
                continue
            if _vocab_ruling_exists(work, vf, cid, bid, src_hash, draft_hash, dict_version):
                continue
            n += 1
        return str(n)
    except Exception:
        return "—"


def _list_incomplete_draft_parts(work: Path) -> list[tuple[str, str, dict]]:
    """未完了（complete でない）の draft-append 記録を列挙する。"""
    out: list[tuple[str, str, dict]] = []
    d = work / "output" / "_work" / "draft_parts"
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("complete") is True:
            continue
        cid = str(data.get("chapter") or "")
        bid = str(data.get("block") or "")
        if cid and bid:
            out.append((cid, bid, data))
    return out


def _status_resume_ticket(work: Path, meta: dict, chapters: list[dict],
                          next_key: str | None, a_done: int, a_total: int) -> list[str]:
    """status の「再開票」節（P2-4）。圧縮後の再開を、記録の読み直しだけで行えるようにする。

    - 工程（分析か翻訳か）と対象ブロック（未完了の先頭）
    - 分析の版（analysis_src_hash の一致状態）と辞書の版（vocab の dict_version）
    - 未裁定の件数（confirm・語彙 candidates は対象ブロックのドラフトから、
      age_policy は作品全体の記録から）
    - draft-append の受領済み範囲（未完了の記録がある場合）

    対象ブロックのコンテキストは毎回読む前提。ここで減らすのは同じ版の
    重複した読み込みだけ（レビュー §3 P2-4）。
    """
    target: tuple[str, str, dict] | None = None
    phase = ""
    if next_key:
        cid, bid = next_key.split("/", 1)
        target, phase = (cid, bid, block_meta(meta, next_key) or {}), "翻訳"
    else:
        found = _find_first_unanalyzed(chapters)
        if found is None and a_done < a_total:
            # フラグと実体（plot）が食い違う部分状態でも対象を出す（推測で黙らない）
            ch0 = chapters[0]
            if ch0.get("blocks"):
                b0 = ch0["blocks"][0]
                found = (ch0["id"], b0["id"], b0)
        if found is not None:
            target, phase = found, "分析"
    if target is None:
        return []  # 未完了ブロックが無い（完成後）は再開票を出さない

    cid, bid, b = target
    key = f"{cid}/{bid}"
    out = ["[status] 再開票（圧縮後の再開用）:"]
    rng = b.get("paras") or []
    rng_s = ""
    if isinstance(rng, (list, tuple)) and len(rng) == 2:
        try:
            rng_s = f"（paras {_para_range_str(int(rng[0]), int(rng[1]))}）"
        except (TypeError, ValueError):
            rng_s = ""
    out.append(f"[status]   工程: {phase} / 対象ブロック: {key}{rng_s}")

    # 分析の版: 記録の analysis_src_hash と現行原文の一致状態
    recorded = str(b.get("analysis_src_hash") or "")
    current = _analysis_src_hash(work, key, meta)
    analyzed = bool(b.get("analysis_committed_at") or b.get("analysis_no_changes"))
    if not analyzed:
        version = ("未分析（--no-analysis で翻訳を許可済み）" if b.get("analysis_skipped")
                   else "未分析")
    elif recorded and current and recorded == current:
        version = f"一致（analysis_src_hash={recorded}）"
    elif recorded and current:
        version = f"不一致（記録 {recorded} != 現行 {current}。分析のやり直しを検討）"
    elif recorded:
        version = f"記録のみ（{recorded}。現行の原文を読めません）"
    else:
        version = "記録なし（旧データ）"
    out.append(f"[status]   分析の版: {version}")

    # 辞書の版（統合辞書。裁定記録の dict_version と同じ値）
    try:
        from vocab import load_vocab
        dict_version = str(load_vocab(work).dict_version())
    except Exception as e:
        dict_version = f"（辞書を読めません: {e}）"
    out.append(f"[status]   辞書の版: {dict_version}")

    # 未裁定: confirm・語彙 candidates は対象ブロックのドラフトから、age_policy は作品全体から
    draft_p = _draft_path(work, cid, bid)
    if draft_p.is_file():
        conf = _count_unacked_confirms(work, cid, bid)
        vocab_n = _count_pending_vocab_candidates(work, meta, cid, bid, draft_p)
        pending_s = f"confirm {conf}件・語彙 candidates {vocab_n}件（対象ブロックのドラフトから検査）"
    else:
        pending_s = "confirm —・語彙 candidates —（ドラフト未配置）"
    out.append(f"[status]   未裁定: {pending_s} / age_policy {_count_unsettled_age_policy(work)}件（作品全体）")

    # draft-append の受領済み範囲（未完了の記録がある場合だけ出す）
    pending_records = _list_incomplete_draft_parts(work)
    if pending_records:
        out.append("[status]   draft-append 受領済み（未完了）:")
        for cid2, bid2, rec in pending_records[:8]:
            covered, missing = _draft_parts_coverage(rec)
            rng2 = rec.get("block_range") or []
            total = ""
            if isinstance(rng2, (list, tuple)) and len(rng2) == 2:
                try:
                    total = f" / 全体 {_para_range_str(int(rng2[0]), int(rng2[1]))}"
                except (TypeError, ValueError):
                    total = ""
            got = _para_ranges_str(covered) or "—"
            lost = _para_ranges_str(missing) or "—"
            out.append(f"[status]     {cid2}/{bid2}: 受領 {got}{total}（残り {lost}）")
        if len(pending_records) > 8:
            out.append(f"[status]     …他{len(pending_records) - 8}件")

    # 台帳（ブロック横断状態の外部化。レビュー提案1）: 消費済みの件数
    try:
        from ledger import ledger_counts
        lc = ledger_counts(work)
    except Exception:
        lc = None
    if lc is not None:
        out.append(f"[status] 台帳: ルビ消費 {lc[0]}件・用語初出 {lc[1]}件・"
                   f"オノマトペ使用 {lc[2]}件")
    return out


def cmd_status(work: Path) -> int:
    started = time.time()
    rc = _cmd_status_impl(work)
    _record_stage(work, "status", None, None, started, rc,
                  reason="success" if rc == 0 else "other")
    return rc


# ---------------------------------------------------------------------------
# proposed サブコマンド（訳語変更の候補の一覧・承認・却下）
# ---------------------------------------------------------------------------

_PROPOSED_LINE_RE = re.compile(
    r"^(?P<key>[A-Za-z_][A-Za-z0-9_]*): 「(?P<old>.*)」→「(?P<new>.*)」\s*$")


def _proposed_entries(work: Path) -> list[tuple[str, dict, str, str]]:
    """proposed が残っているエントリを (種別, エントリ, 訳語キー, 原語キー) で返す。"""
    out: list[tuple[str, dict, str, str]] = []
    for e in load_characters(work).get("characters") or []:
        if str(e.get("proposed") or "").strip():
            out.append(("characters", e, "name_ja", "name_src"))
    for e in load_terms(work).get("terms") or []:
        if str(e.get("proposed") or "").strip():
            out.append(("terms", e, "term_ja", "term_src"))
    return out


def _find_proposed_in(data: dict, kind: str, name: str) -> dict | None:
    """読み込んだ分析データの中から、指定名のエントリを返す（その場で書き換える）。"""
    arr_key = "characters" if kind == "characters" else "terms"
    ja_key, src_key = (("name_ja", "name_src") if kind == "characters"
                       else ("term_ja", "term_src"))
    for e in data.get(arr_key) or []:
        if name in (str(e.get(ja_key) or ""), str(e.get(src_key) or "")):
            return e
    return None


def _proposed_lines(entry: dict) -> list[str]:
    return [ln for ln in str(entry.get("proposed") or "").splitlines() if ln.strip()]


def _load_analysis_data(work: Path, kind: str) -> dict:
    return load_characters(work) if kind == "characters" else load_terms(work)


def _save_analysis_data(work: Path, kind: str, data: dict) -> None:
    save_toml(_analysis_path(work, kind), data)


def cmd_proposed_list(work: Path) -> int:
    rows = _proposed_entries(work)
    if not rows:
        print("[proposed] 滞留なし")
        return 0
    for kind, e, ja_key, src_key in rows:
        name = e.get(ja_key) or e.get(src_key) or "?"
        print(f"[proposed] {name} ({kind})")
        for ln in _proposed_lines(e):
            print(f"[proposed]   {ln}")
    print("[proposed] 承認: saga.py proposed approve <work> --name <訳語> --key <キー>"
          " ／ 却下: ... reject ...")
    return 0


def _apply_proposed(work: Path, name: str, key: str, approve: bool, reason: str) -> int:
    # candidates: proposed を持つエントリだけを対象にする
    for kind in ("characters", "terms"):
        data = _load_analysis_data(work, kind)
        entry = _find_proposed_in(data, kind, name)
        if entry is not None and str(entry.get("proposed") or "").strip():
            break
        entry = None
    if entry is None:
        print(f"[proposed] エラー: proposed を持つエントリが見つかりません: {name}", file=sys.stderr)
        return 1
    lines = _proposed_lines(entry)
    idx = None
    parsed = None
    for i, ln in enumerate(lines):
        m = _PROPOSED_LINE_RE.match(ln)
        if m and m.group("key") == key:
            idx, parsed = i, m
    if idx is None or parsed is None:
        keys = sorted({m.group("key") for ln in lines if (m := _PROPOSED_LINE_RE.match(ln))})
        print(f"[proposed] エラー: キー '{key}' の提案がありません（候補: {', '.join(keys) or 'なし'}）",
              file=sys.stderr)
        return 1
    old, new = parsed.group("old"), parsed.group("new")
    if approve:
        entry[key] = new
    del lines[idx]
    if lines:
        entry["proposed"] = "\n".join(lines)
    else:
        entry.pop("proposed", None)
    _save_analysis_data(work, kind, data)
    append_jsonl(_analysis_edits_path(work), {
        "ts": _now_iso(), "via": "proposed-approve" if approve else "proposed-reject",
        "match": name, "key": key, "old": old, "new": new, "reason": reason,
    })
    action = "承認" if approve else "却下"
    print(f"[proposed] {action}: {name} / {key}")
    if approve:
        print(f"[proposed]   {key}: 「{old}」→「{new}」")
        if key in ("name_ja", "term_ja") and old != new:
            print(f"[proposed] 訳文へ伝播するには: "
                  f"python3 scripts/rename_terms.py <work> --term {old} --to {new} --apply")
    else:
        print(f"[proposed]   提案を破棄しました（値は変更していません）")
    return 0


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

    from vocab import house_style_misplaced
    _mp = house_style_misplaced(work)
    if _mp is not None:
        print(f"[status] 警告: {_mp} は読まれません（探索先は _house/house_style.toml）。"
              "`python3 scripts/house_style.py init <work>` で正しい場所に作れます")

    # 本文の二重取り込み（split の重複検査）を承知で続行した作品は、毎回出す。
    # 弱いモデルが「同じ話を2回訳す」事故を再発させないための常設の注意。
    dup = (meta.get("work") or {}).get("duplicate_check") or {}
    if dup.get("accepted") and float(dup.get("ratio") or 0) >= 0.10:
        print(f"[status] 警告: 本文の重複率 {float(dup['ratio']):.0%} を承知で分割されています"
              "（--accept-duplicate。同じ内容を二重に訳さないか確認）")

    assembled_at, assembled_outputs = _assembled_state(work)
    # 改名後の stale（派生物の再生成と検査記録の再検収が必要。P0-6・#22）。
    # 再 assemble の記録が stale より新しければ解消済みとして扱う。
    stale_at = derivatives_stale_at(work)
    stale_active = bool(stale_at) and not (assembled_at and assembled_at > stale_at)
    stale_blocks = stale_after_rename_blocks(meta) if stale_active else []
    if assembled_at:
        # assemble 済み: 中間物は削除済みで、ブロック別出力による進捗数は
        # 未完了に見えてしまう。完成状態を明示して誤認を防ぐ（#20）。
        print(f"[status] work={work.name}: 出力済み（assemble 済み・機械検査のみ。検収は未実施）")
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
            print("[status] 次の作業: 検収（saga.py review → 修正 → 再 assemble）。"
                  "機械 PASS は品質の保証ではありません")
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

    # 命名方針の未決定警告（P0-5）: lock_prep 済みで方針が未記録のまま、未訳
    # ブロックが残っている work では、次の確定の前に決めるよう促す。分析の1周を
    # 待つと、古い訳語で続きが書かれてしまう。決定は house_style の [naming_policy]。
    if t_done < t_total and _analysis_phase_active(work) and _naming_policy_of(work) is None:
        print("[status] 命名方針: 未決定（未訳ブロック残。次の確定前に決めてください）")

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

    # 再開票（圧縮後の再開用。P2-4）
    for line in _status_resume_ticket(work, meta, chapters, next_key, a_done, a_total):
        print(line)

    return 0


# ---------------------------------------------------------------------------
# 残渣検査の共通収集と lint サブコマンド（P0-3）
# ---------------------------------------------------------------------------
#
# lint は「analyze-commit が保存前に拒否する残渣」を commit の前に一覧化する
# 読み取り専用コマンド。検出は analyze-commit と同一の find_cn_residue_fields
# （構造で対象欄を選ぶ既存実装）をそのまま使い、分岐実装は作らない。
# 検査対象は analyze-commit が保存前に検査する3ファイル（characters / terms /
# plot）と、--delta で渡された差分 TOML。
#
# 原文側（値が原語）として除外するキーの正:
#   - data-schema.md の src キー: name_src / term_src / title_src / src /
#     target_src / similar_to_src（+ aliases は原文の異表記）
#     → detect.zh2ja.find_cn_residue_fields の _is_src_key が除外する
#   - dismiss_variant / dismissed_variants / dismiss_reason（5b。相手の原文名を
#     保持する欄）→ collect_analysis_residue が検査ビューから外す
#   - 差分 TOML 固有: match_key / match_value / block（照合キーとブロック指定）
#     → 差分を検査するときだけ DELTA_SRC_KEYS で除外する

#: 差分 TOML で値が原語（原文側）になるキー（lint --delta の検査から除外）。
DELTA_SRC_KEYS = frozenset({"match_key", "match_value", "block"})

def collect_analysis_residue(
        entries: list[tuple[str, dict, str]]) -> list[tuple[str, int, str, str, str]]:
    """分析 TOML の残渣を収集する（analyze-commit / lint / prepare 警告の共通経路）。

    ``entries`` は ``(表示名, 検査対象データ, 行番号を探すテキスト)``。検出は
    ``detect.zh2ja.find_cn_residue_fields``（analyze-commit が保存前に使うのと
    同じ関数。原文側の欄は `_is_src_key` が除外する）。戻り値は
    ``(名前, 行, キー, 理由, 場所)`` の一覧。
    """
    from detect.zh2ja import find_cn_residue_fields

    out: list[tuple[str, int, str, str, str]] = []
    for name, data, text in entries:
        for f in find_cn_residue_fields(data):
            line, key = _residue_line_key(f, text)
            out.append((name, line, key, f.value, f.context or ""))
    return out


def _residue_line_key(finding, text: str) -> tuple[int, str]:
    """Finding に (行番号, キー名) を付ける。

    Finding.context は find_cn_residue_fields が作る ``"path = 値…"`` 形式。
    ``text``（TOML テキスト）から値の先頭部分を探し、最初に一致した行を返す
    （見つからなければ 0 = 不明。値の推測はしない）。
    """
    ctx = finding.context or ""
    loc, sep, snip = ctx.partition(" = ")
    if not sep:
        loc, snip = "", ctx
    key = loc.rsplit(".", 1)[-1] if loc else ""
    key = re.sub(r"\[\d+\]$", "", key)
    line = 0
    first = snip.splitlines()[0] if snip else ""
    if first:
        for i, ln in enumerate(text.splitlines(), 1):
            if first in ln:
                line = i
                break
    return line, key


def _load_residue_targets(work: Path) -> tuple[list[tuple[str, dict, str]], list[str]]:
    """残渣検査の対象（analyze-commit が保存前に検査する3ファイル）を読む。

    戻り値: ``(エントリ, 読み取りエラー)``。エントリは
    ``(表示名, パース済みデータ, 行番号探索用テキスト)``。存在しないファイルは
    対象外（残渣なしと矛盾しない）。壊れたファイルはエラーとして返す。
    """
    entries: list[tuple[str, dict, str]] = []
    errors: list[str] = []
    for kind in ("characters", "terms", "plot"):
        p = _analysis_path(work, kind)
        if not p.is_file():
            continue
        try:
            data = load_toml(p)
        except Exception as e:
            errors.append(f"{p.name} を読めません: {e}")
            continue
        if not isinstance(data, dict) or not data:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            errors.append(f"{p.name} を読めません: {e}")
            continue
        entries.append((p.name, data, text))
    return entries, errors


def _delta_residue_view(delta: dict) -> dict:
    """差分 TOML を残渣検査用のビューに写す。

    match_key / match_value / block の値は原文側（照合キー・ブロック指定）
    なので空にして検査対象から外す。通常の src キー（src / *_src / aliases）は
    find_cn_residue_fields 側が構造で除外する。
    """
    view = copy.deepcopy(delta)

    def scrub(obj) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in DELTA_SRC_KEYS and isinstance(v, str):
                    obj[k] = ""
                else:
                    scrub(v)
        elif isinstance(obj, list):
            for x in obj:
                scrub(x)

    scrub(view)
    return view


def _analysis_residue_warning(work: Path) -> str | None:
    """prepare --mode analysis の残渣の事前警告（P0-3）。1行だけ返す（無ければ None）。"""
    try:
        entries, _errors = _load_residue_targets(work)
        hits = collect_analysis_residue(entries)
    except Exception:
        return None
    if not hits:
        return None
    names = sorted({h[0] for h in hits})
    shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
    return (f"[prepare] 警告: 既存の分析 TOML に日本語フィールドの残渣が {len(hits)} 件あります"
            f"（{shown}）。saga.py lint で一覧し、修正してから分析してください"
            f"（日本語フィールドは訳語のみ。原語は *_src / match_value 等）")


def cmd_lint(work: Path, delta: Path | None = None) -> int:
    """分析 TOML の日本語フィールド残渣の事前一覧（読み取り専用。P0-3）。

    ファイルは書かない（ロック・計測記録も取らない）。残渣が無ければ exit 0、
    あれば exit 1 で、日本語フィールドは訳語のみで書くよう案内する。
    """
    if not work.is_dir():
        print(f"[lint] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    entries, errors = _load_residue_targets(work)
    for e in errors:
        print(f"[lint] エラー: {e}", file=sys.stderr)

    hits = collect_analysis_residue(entries)

    if delta is not None:
        if not delta.is_file():
            print(f"[lint] エラー: delta ファイルが見つかりません: {delta}", file=sys.stderr)
            return 1
        try:
            d_data = load_toml(delta)
        except Exception as e:
            print(f"[lint] エラー: delta を読めません: {e}", file=sys.stderr)
            return 1
        try:
            d_text = delta.read_text(encoding="utf-8")
        except OSError:
            d_text = ""
        hits += collect_analysis_residue(
            [(delta.name, _delta_residue_view(d_data), d_text)])

    if hits:
        for name, line, key, reasons, _ctx in hits:
            at = f"{name}:{line}" if line else name
            print(f"[lint] {at}: {key or '-'}: {reasons}")
        print(f"[lint] {len(hits)} 件。日本語フィールドは訳語のみで書いてください"
              "（原語は *_src / match_value 等にだけ書く）", file=sys.stderr)
        return 1
    if errors:
        return 1
    print("[lint] 残渣なし（analysis/*.toml の日本語フィールド）")
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


#: --allow-protected-overwrite で上書きを許す記述系キー（P1-5）。
#: 訳語そのもの（name_ja / term_ja）と声・ルビの欄は引き続き proposed へ退避する
#: （訳語の変更は rename_terms.py の領分）。
PROTECTED_OVERWRITABLE_KEYS = frozenset(
    {"memo", "role", "appearance", "relationships", "note"})


def _analysis_edits_path(work: Path) -> Path:
    """分析の訂正監査記録（_work/analysis_edits.jsonl。purge しない。P1-5）。"""
    return work / "_work" / "analysis_edits.jsonl"


def _record_protected_overwrite(edits_out: list[dict] | None, match: str,
                                key: str, old, new) -> None:
    """保護キーの明示上書きを監査記録として集める（P1-5。old == new は記録しない）。"""
    if edits_out is None or str(old) == str(new):
        return
    edits_out.append({"ts": _now_iso(), "match": match, "key": key,
                      "old": old, "new": new, "via": "protected-overwrite"})


# ── 異表記候補の別人棄却（dismiss_variant。5b） ──────────────────────────
# 近い表記の名前が原文に併存するときの「異表記候補」は、aliases に入れるまで
# 残る。別人（同音・類似だが別の人物）だと確定した場合は dismiss_variant で
# 相手の name_src を指定して棄却を記録し、該当ペアを候補から落として再検出を
# 抑止する。棄却は対象キャラの dismissed_variants（相手名の文字列配列）に残し、
# 監査は _work/analysis_edits.jsonl へ via="variant-dismiss" の1行で記録する。

def _dismissed_variants_of(entry: dict) -> list[str]:
    """キャラの `dismissed_variants`（別人として棄却した相手名）を返す。無ければ空。"""
    v = entry.get("dismissed_variants")
    if not isinstance(v, list):
        return []
    return [x.strip() for x in v if isinstance(x, str) and x.strip()]


def _merge_dismissed_variants(existing, names) -> list[str]:
    """dismissed_variants の追記マージ（既存の順を保ち、重複は足さない）。"""
    out: list[str] = list(existing) if isinstance(existing, list) else []
    seen = {v.strip() for v in out if isinstance(v, str) and v.strip()}
    values = names if isinstance(names, list) else [names]
    for v in values:
        if not isinstance(v, str) or not v.strip():
            continue
        if v.strip() in seen:
            continue
        seen.add(v.strip())
        out.append(v.strip())
    return out


def _pair_dismissed(a: dict, b: dict) -> bool:
    """a と b のペアが「別人」として棄却済みか（相互の名前を照合する）。

    片方が相手の名前（name_src / name_ja / ruby / aliases のいずれか）を
    dismissed_variants に持てば、そのペアは棄却済みとして扱う。
    """
    dv_a = set(_dismissed_variants_of(a))
    dv_b = set(_dismissed_variants_of(b))
    if not dv_a and not dv_b:
        return False
    if dv_a & set(_name_forms(b)):
        return True
    if dv_b & set(_name_forms(a)):
        return True
    return False


def _candidate_dismissed(cand: dict, entry: dict, owners: dict[str, dict]) -> bool:
    """キャラ候補が entry 側から見て棄却済みか（dismiss_variant）。

    entry 自身が候補の相手名を棄却済み、または相手キャラが entry の名前を
    棄却済みなら真（該当キャラが見つからない揺れ表記は名前の直接照合のみ）。
    """
    other = cand["name_src"]
    if other and other in _dismissed_variants_of(entry):
        return True
    owner = owners.get(other)
    if owner is not None and _pair_dismissed(entry, owner):
        return True
    return False


def _work_candidate_dismissed(cand: dict, owners: dict[str, dict]) -> bool:
    """work レベル候補が棄却済みか（dismiss_variant）。

    既知側（similar_to_src / name_src のうち人物表に居る方）の持ち主が
    もう一方の名前を棄却済みなら真。
    """
    for known, other in ((cand["similar_to_src"], cand["name_src"]),
                         (cand["name_src"], cand["similar_to_src"])):
        owner = owners.get(known)
        if owner is None or not other:
            continue
        if other in _dismissed_variants_of(owner):
            return True
        other_owner = owners.get(other)
        if other_owner is not None and _pair_dismissed(owner, other_owner):
            return True
    return False


def _apply_variant_dismissal(chars: list[dict], target: dict, partner_name,
                             reason: str, match_value: str,
                             chars_data: dict | None,
                             edits_out: list[dict] | None) -> str | None:
    """別人の棄却（dismiss_variant）を適用する。変更があればログ1行を返す。

    - 対象キャラの ``dismissed_variants`` へ相手名を追記する
    - 対象・相手（相手がキャラのとき）・work レベルの variant_candidates から
      該当ペアを除去する
    - 監査記録（``via="variant-dismiss"``）を ``edits_out`` に1行だけ追加する

    相手名が人物表の name_src にも既存の異表記候補にも無い場合は ValueError
    （存在しない相手名は黙殺しない）。再検出の抑止は _record_voice_candidates。
    """
    name = str(partner_name or "").strip()
    target_src = str(target.get("name_src") or "").strip()
    if not name:
        raise ValueError("dismiss_variant は相手の name_src（空でない文字列）が必要です")
    if name == target_src:
        raise ValueError(f"dismiss_variant: 自分自身（{name}）は別人として棄却できません")
    partner = next((c for c in chars if isinstance(c, dict)
                    and str(c.get("name_src") or "").strip() == name), None)

    # 相手名は「人物表の name_src」か「既存の異表記候補に出る名前」のどちらかに
    # 実在することを要求する（存在しない名前はタイプミスとして拒否）
    known_names: set[str] = set()
    for c in chars:
        if not isinstance(c, dict):
            continue
        known_names.add(str(c.get("name_src") or "").strip())
        for cand in variant_candidates_of(c):
            known_names.add(cand["name_src"])
            known_names.add(cand["similar_to_src"])
    if isinstance(chars_data, dict):
        for cand in normalize_variant_candidates(chars_data.get("variant_candidates")):
            known_names.add(cand["name_src"])
            known_names.add(cand["similar_to_src"])
    known_names.discard("")
    if name not in known_names:
        raise ValueError(
            f"dismiss_variant: 相手の name_src「{name}」が人物表にも異表記候補にも"
            "見つかりません（存在しない相手名の棄却はできません）")

    changed = False
    if name not in _dismissed_variants_of(target):
        target["dismissed_variants"] = _merge_dismissed_variants(
            target.get("dismissed_variants"), [name])
        changed = True

    target_forms = set(_name_forms(target))
    partner_forms = set(_name_forms(partner)) if partner is not None else {name}

    def _is_pair(cand: dict) -> bool:
        """候補の両端が対象と相手にまたがるか（該当ペア）。"""
        ends = {cand["name_src"], cand["similar_to_src"]} - {""}
        return bool(ends & target_forms) and bool(ends & partner_forms)

    def _strip(holder: dict) -> bool:
        raw = holder.get("variant_candidates")
        if not isinstance(raw, list) or not raw:
            return False
        kept = [c for c in variant_candidates_of(holder) if not _is_pair(c)]
        if len(kept) == len(variant_candidates_of(holder)):
            return False
        if kept:
            holder["variant_candidates"] = kept
        else:
            holder.pop("variant_candidates", None)
        return True

    if _strip(target):
        changed = True
    if partner is not None and partner is not target and _strip(partner):
        changed = True
    if isinstance(chars_data, dict) and isinstance(
            chars_data.get("variant_candidates"), list):
        work = normalize_variant_candidates(chars_data["variant_candidates"])
        kept_w = [c for c in work if not _is_pair(c)]
        if len(kept_w) != len(work):
            if kept_w:
                chars_data["variant_candidates"] = kept_w
            else:
                chars_data.pop("variant_candidates", None)
            changed = True

    if not changed:
        return None
    if edits_out is not None:
        rec = {"ts": _now_iso(), "match": match_value, "key": "dismiss_variant",
               "old": "", "new": name, "via": "variant-dismiss"}
        if reason:
            rec["reason"] = reason
        edits_out.append(rec)
    return (f"異表記棄却: {target_src or match_value}⇔{name}"
            "（別人として記録。dismissed_variants に追記し、再検出しない）")


def _apply_chars_delta(chars: list[dict], delta_list: list[dict],
                       allow_protected_overwrite: bool = False,
                       edits_out: list[dict] | None = None,
                       chars_data: dict | None = None) -> list[str]:
    """キャラ差分を適用。proposed が発生した変更を説明する文字列リストを返す。

    aliases は既存への追記マージ（重複排除）。voice_status = confirmed への
    遷移は声の記述が必要で、無ければ ValueError（P0-4）。
    ``allow_protected_overwrite``（P1-5）では記述系キー
    （PROTECTED_OVERWRITABLE_KEYS）だけ proposed へ退避せず本体を更新し、
    監査記録を ``edits_out`` に集める（name_ja 等は従来どおり proposed）。
    ``dismiss_variant``（5b）は相手の name_src を ``dismissed_variants`` へ
    追記し、該当ペアを variant_candidates から除去する。work レベルの候補の
    除去には ``chars_data``（characters TOML 全体）を使う（無ければ char のみ）。
    """
    proposed_log: list[str] = []
    SPECIAL = {"match_key", "match_value", "updates", "new_fields",
               "characters_delta", "terms_delta", "plot_delta", "block",
               "dismiss_variant", "dismiss_reason"}
    protect_keys = {"name_ja", "role", "first_person", "second_person",
                    "speech_style", "vocab", "catchphrase", "ruby"}
    for delta in delta_list:
        match_key = delta.get("match_key", "name_src")
        match_value = delta.get("match_value", "")
        # 別人の棄却は updates / new_fields 経由でも受け付ける
        nested = dict(delta.get("updates") or {})
        nested.update(delta.get("new_fields") or {})
        dismiss_variant = nested.get("dismiss_variant", delta.get("dismiss_variant"))
        dismiss_reason = nested.get("dismiss_reason", delta.get("dismiss_reason"))
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
            # 棄却の指定はフィールドとして書かず、更新適用の後に専用処理する
            updates.pop("dismiss_variant", None)
            updates.pop("dismiss_reason", None)
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
                    if str(existing).strip() == str(v).strip():
                        continue  # 同一値の再送は提案にしない（proposed の空回り防止）
                    if allow_protected_overwrite and k in PROTECTED_OVERWRITABLE_KEYS:
                        # 記述系キーの明示上書き（proposed に退避せず、監査記録に残す。P1-5）
                        _record_protected_overwrite(edits_out, match_value, k, existing, v)
                        entry[k] = v
                        continue
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
            base.pop("dismiss_variant", None)
            base.pop("dismiss_reason", None)
            if "aliases" in base:
                base["aliases"] = _merge_aliases([], base["aliases"])
            _check_voice_transition(base, base, match_value)
            chars.append(base)
            entry = base
        # 別人の棄却（dismiss_variant）: 対象の更新後に適用する
        if dismiss_variant is not None:
            log = _apply_variant_dismissal(
                chars, entry, dismiss_variant,
                dismiss_reason if isinstance(dismiss_reason, str) else "",
                match_value, chars_data, edits_out)
            if log:
                proposed_log.append(log)
    return proposed_log


def _apply_terms_delta(terms: list[dict], delta_list: list[dict],
                       allow_protected_overwrite: bool = False,
                       edits_out: list[dict] | None = None) -> list[str]:
    """用語差分を適用。proposed ログを返す。

    ``allow_protected_overwrite``（P1-5）では記述系キー
    （PROTECTED_OVERWRITABLE_KEYS。terms では memo）だけ proposed へ退避せず
    本体を更新し、監査記録を ``edits_out`` に集める（term_ja 等は従来どおり）。
    """
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
                    if str(existing).strip() == str(v).strip():
                        continue  # 同一値の再送は提案にしない（proposed の空回り防止）
                    if allow_protected_overwrite and k in PROTECTED_OVERWRITABLE_KEYS:
                        # 記述系キーの明示上書き（proposed に退避せず、監査記録に残す。P1-5）
                        _record_protected_overwrite(edits_out, match_value, k, existing, v)
                        entry[k] = v
                        continue
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


def _apply_plot_delta(entries: list[dict], delta_list: list[dict],
                      edits_out: list[dict] | None = None) -> list[str]:
    """プロットエントリを追記（既定）。``replace = true`` は同一 block の text を置換（P1-5）。

    冪等性: 追記は同一 block + 同一 text を重複スキップ。置換は同一 block の
    先頭エントリの text を置き換える（同一 block のエントリが無ければ追記に回す）。
    置換の監査記録は ``edits_out`` に集める。
    """
    proposed_log: list[str] = []
    for delta in delta_list:
        block = delta.get("block", "")
        text = (delta.get("updates") or {}).get("text") or delta.get("text", "")
        if delta.get("replace") is True and block:
            # 同一 block の text を置換（既定は従来どおり追記。P1-5）
            idx = next((i for i, e in enumerate(entries)
                        if str(e.get("block", "")).strip() == block.strip()), None)
            if idx is not None:
                old = str(entries[idx].get("text", ""))
                entries[idx]["text"] = text
                if edits_out is not None and old != str(text):
                    edits_out.append({"ts": _now_iso(), "match": block, "key": "text",
                                      "old": old, "new": text, "via": "plot-replace"})
                continue
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


def _tag_edits(edits: list[dict], start: int, file_name: str) -> None:
    """``edits[start:]`` に書き込み先ファイル名を付ける（P1-5 の監査記録用）。"""
    for e in edits[start:]:
        e.setdefault("file", file_name)


def _build_analysis_payloads(work: Path, delta: dict,
                             allow_protected_overwrite: bool = False,
                             edits_out: list[dict] | None = None
                             ) -> tuple[list[tuple[Path, dict]] | None, list[str], list[str]]:
    """差分を適用した書き込み対象（パス, データ）を組み立てる。書き込みは行わない。

    戻り値 ``(payloads, logs, errors)``:

    - ``payloads``: 成功時のみ ``[(path, data), ...]``。失敗時は ``None``。
    - ``logs``: 適用ログ（proposed 退避など）
    - ``errors``: 検証・構築に失敗した理由（成功時は空）

    反映対象（characters / terms / plot）を**すべて先に組み立て・検証**する。
    ここで例外が出れば一切書き込まずに ``payloads=None`` を返す。
    ``allow_protected_overwrite`` / ``edits_out`` は保護キーの明示上書きの
    監査記録（P1-5）。記録は呼び出し側が保存成功後に書き出す。
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
    edits: list[dict] = edits_out if edits_out is not None else []
    try:
        n0 = len(edits)
        cl = _apply_chars_delta(chars_list, delta.get("characters_delta", []),
                                allow_protected_overwrite=allow_protected_overwrite,
                                edits_out=edits, chars_data=chars)
        _tag_edits(edits, n0, _analysis_path(work, "characters").name)
        n1 = len(edits)
        tl = _apply_terms_delta(terms_list, delta.get("terms_delta", []),
                                allow_protected_overwrite=allow_protected_overwrite,
                                edits_out=edits)
        _tag_edits(edits, n1, _analysis_path(work, "terms").name)
        n2 = len(edits)
        pl = _apply_plot_delta(plot_entries, delta.get("plot_delta", []), edits_out=edits)
        _tag_edits(edits, n2, _analysis_path(work, "plot").name)
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


def _block_src_items(work: Path, block_key: str, meta: dict) -> list[tuple[int, str]]:
    """block 原文の (段落番号, 本文) 列。読めなければ空列。

    辞書の fixed 検査を段落単位で行う（原文段落に語が出ているのに、対応する訳文
    段落に規定訳が無い → 違反）ために使う（check_draft の src_items）。
    """
    b = block_meta(meta, block_key)
    if b is None:
        return []
    cid = block_key.split("/", 1)[0]
    try:
        paras_data = load_paras(work, cid)
    except (FileNotFoundError, OSError, ValueError):
        return []
    return [(int(p["n"]), str(p.get("text", ""))) for p in block_paras(paras_data, b)]


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


#: plot_delta エントリの許容キー（P1-5。replace は同一 block の text 置換）。
#: 未知キーは黙って無視せず拒否する。
PLOT_DELTA_KEYS = frozenset({"block", "text", "replace", "updates", "new_fields"})


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
    4. plot エントリの ``block`` は delta の block と一致。``replace`` は
       boolean。未知キーは拒否（許容: PLOT_DELTA_KEYS）
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
        # 追加欄の型検証（aliases / voice_status / voice_note / dismiss_variant。P0-4 / 5b）
        for key in ("voice_status", "voice_note", "aliases",
                    "dismiss_variant", "dismiss_reason"):
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
                if key == "dismiss_variant" and (not isinstance(v, str) or not v.strip()):
                    return False, f"{where} は相手の name_src（空でない文字列）であるべきです", False
                if key == "dismiss_reason" and not isinstance(v, str):
                    return False, f"{where} は文字列であるべきです", False
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
        if "replace" in p and not isinstance(p["replace"], bool):
            return False, f"plot_delta[{i}].replace は true / false（boolean）であるべきです", False
        unknown = [k for k in p if k not in PLOT_DELTA_KEYS]
        if unknown:
            return False, (f"plot_delta[{i}] に未知のキー `{unknown[0]}` があります"
                           f"（許容: {' / '.join(sorted(PLOT_DELTA_KEYS))}）"), False

    has_changes = any(len(delta.get(k, [])) > 0
                      for k in ("characters_delta", "terms_delta", "plot_delta"))
    if not has_changes and delta.get("no_changes") is not True:
        return False, ("差分が空です。分析の結果として変更が無い場合は `no_changes = true` を"
                       "明示してください（黙っての空コミットは拒否）"), False

    return True, None, has_changes


def cmd_analyze_commit(work: Path, delta_path: Path,
                       force_residue: bool = False,
                       allow_protected_overwrite: bool = False) -> int:
    started = time.time()
    rc = _cmd_analyze_commit_impl(work, delta_path, force_residue, allow_protected_overwrite)
    reason = "success" if rc == 0 else "other"
    _record_stage(work, "analyze-commit", None, None, started, rc, reason=reason)
    return rc


def _cmd_analyze_commit_impl(work: Path, delta_path: Path,
                             force_residue: bool = False,
                             allow_protected_overwrite: bool = False) -> int:
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
            print(f"[analyze-commit] エラー: 拒否しました: {reason}。"
                  f"次の一手: delta の block・キー・値を修正して再実行してください"
                  f"（この時点では何も保存されません）", file=sys.stderr)
            return 1

        # 反映後の仮の全体を組み立てる（書き込みはまだ行わない）。
        # 分析工程に入った work では、対象言語の欄（*_src 以外）に中国語が残って
        # いないかを保存の前に検査する（P0-4 / #8）。行ではなく TOML の構造で
        # 対象の欄を選ぶ（find_cn_residue_fields）。
        check_active = _analysis_phase_active(work)
        payloads: list[tuple[Path, dict]] | None = None
        content_changed = False
        logs: list[str] = []
        candidate_logs: list[str] = []
        candidates_changed = False
        edits: list[dict] = []
        if has_changes or check_active:
            payloads, logs, errors = _build_analysis_payloads(
                work, delta,
                allow_protected_overwrite=allow_protected_overwrite,
                edits_out=edits)
            if payloads is None:
                for e in errors:
                    print(f"[analyze-commit] エラー: {e}", file=sys.stderr)
                det = _fmt_items([str(e) for e in errors]) if errors else ""
                print(f"[analyze-commit] エラー: マージ検証失敗、何も書きませんでした。"
                      + (f"検出: {det}。" if det else "")
                      + f"次の一手: delta の記述を修正して再実行してください"
                      f"（何も保存されていません）", file=sys.stderr)
                return 1
            # 実際に内容が変わるか（同じ差分の再投入を「マージした」と
            # 誤認させないため、保存前の内容と比較する）
            content_changed = False
            for _p, _d in payloads:
                try:
                    _old = _p.read_text(encoding="utf-8") if _p.is_file() else None
                except OSError:
                    _old = None
                if _old != dump_toml(_d):
                    content_changed = True
                    break
            # 確認の候補（話者・異表記）: 差分適用後のデータと対象ブロックの
            # 原文から抽出して記録する（確定はしない。P0-4・#7）
            candidate_logs, candidates_changed = _record_voice_candidates(
                work, block, meta, payloads)

        forced_residue = False
        if check_active and payloads is not None:
            # 残渣収集は lint / prepare 警告と共通（collect_analysis_residue。P0-3）。
            # 行番号は保存される形（dump_toml）の上で数える。
            residue = collect_analysis_residue(
                [(p.name, d, dump_toml(d)) for p, d in payloads])
            if residue:
                for r_name, r_line, r_key, r_reasons, r_ctx in residue[:20]:
                    at = f"{r_name}:{r_line}" if r_line else r_name
                    print(f"[analyze-commit] 残渣: {at}: {r_key or '-'}: {r_reasons} ({r_ctx})")
                if len(residue) > 20:
                    print(f"[analyze-commit] … 他 {len(residue) - 20} 件")
                if not force_residue:
                    det = _fmt_items([
                        (f"{r_name}:{r_line}" if r_line else r_name)
                        for r_name, r_line, _k, _rs, _c in residue])
                    print(
                        f"[analyze-commit] エラー: 分析 TOML の対象欄に中国語の残渣が "
                        f"{len(residue)} 件あります: {det}。"
                        f"日本語フィールド（訳語側）を修正して再実行するか、"
                        f"承知の上で通す場合だけ --force-residue を明示してください"
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

        if content_changed or candidates_changed:
            write_errors = _commit_analysis_payloads(payloads, work)
            if write_errors:
                for e in write_errors:
                    print(f"[analyze-commit] エラー: {e}", file=sys.stderr)
                det = _fmt_items([str(e) for e in write_errors])
                print(f"[analyze-commit] エラー: マージ検証失敗、何も書きませんでした。"
                      f"検出: {det}。次の一手: delta の記述を修正して再実行してください"
                      f"（何も保存されていません）", file=sys.stderr)
                return 1
            # 監査記録（P1-5）: 保護キーの明示上書き・plot 置換を保存成功後に1行ずつ残す
            for e in edits:
                append_jsonl(_analysis_edits_path(work), e)
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
            n_prop = len([l for l in logs if "→proposed" in l])
            if content_changed:
                print(f"[analyze-commit] 完了: 差分 {n} 件を適用"
                      f"（{n_prop} 件は proposed に退避）")
            else:
                print(f"[analyze-commit] 完了: 差分 {n} 件はすべて既存値と同じでした"
                      "（内容に変化なし・proposed 追加なし）")
        else:
            print("[analyze-commit] 完了: 差分なし（analysis_no_changes = true を記録）")
        if edits:
            print(f"[analyze-commit] 監査記録: {len(edits)} 件を _work/analysis_edits.jsonl に追記"
                  "（保護キーの明示上書き・plot 置換・異表記棄却）")
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


#: 異表記候補にしないノイズ片（G38: 见时/小腹/小心/的时 などが人物として
#: 登録された実害）。追加は templates/brief_analyze.md「variant登録基準」に従う。
_VARIANT_NOISE_FORMS = frozenset({"见时", "小腹", "小心", "的时"})
#: 機能字のみで成る片は異表記候補にしない（付属語・量詞・方位詞の断片）。
_VARIANT_FUNC_CHARS = frozenset("的了是在和与或个一种每这那哪何大小多少时分年日月人中上下来去出入好坏新老我你她他其所另")


def _is_noise_variant(t: str) -> bool:
    """work レベルの異表記候補から外す片か（G38 の除外条件）。"""
    if t in _VARIANT_NOISE_FORMS:
        return True
    return len(t) >= 2 and all(ch in _VARIANT_FUNC_CHARS for ch in t)


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
      として記録し、aliases に入るか dismiss_variant で別人と記録するまで残す
      （解決済み・棄却済みは自動で落とし、再検出もしない）。該当キャラが無い
      候補は work レベル（characters TOML トップ）に置く。

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

    # 解決済みの異表記候補を先に落とす（aliases へ移った・相手の別名になった・
    # dismiss_variant で別人として棄却済み）
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
            if _candidate_dismissed(cand, c, owners):
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
                    if _pair_dismissed(ca, cb):
                        continue  # 別人として棄却済み（dismiss_variant）
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
                            f"（同一人物の異表記の可能性。aliases に入れるか"
                            f"dismiss_variant で別人と記録するまで残す）")

    # ③ 異表記候補（原文側だけに近い表記がある。該当キャラが無い → work レベル）
    raw_work = chars_data.get("variant_candidates")
    work_cands = normalize_variant_candidates(raw_work)
    kept_work = [x for x in work_cands
                 if x["name_src"] not in known
                 and not _work_candidate_dismissed(x, owners)]
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
                if _is_noise_variant(t):
                    continue  # G38: 一般名詞断片を人物候補にしない
                if not _similar_names(n, t):
                    continue
                if t in _dismissed_variants_of(_c):
                    continue  # 別人として棄却済み（dismiss_variant）
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
                        f"aliases に入れるか dismiss_variant で別人と記録するまで残す）")
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


def _report_count(value) -> int:
    """report.json の検出件数を読む（#73: 明細配列と旧形式の件数値の両対応）。"""
    if isinstance(value, (list, tuple)):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _classify_commit_failure(snap_dir: Path) -> str:
    """commit 失敗時の理由を種類別に分類する。

    検査ごとの検出を記録した report.json を優先して読み、無ければ "other"。
    件数の欄は明細配列（新形式・#73）と数値（旧形式）の両方を読める。
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
    if isinstance(align, dict) and _report_count(align.get("fatal")) > 0:
        return "align"
    if isinstance(align, dict) and _report_count(align.get("confirm_unacked")) > 0:
        return "confirm_unacked"
    qa = stages.get("qa")
    if isinstance(qa, dict) and qa.get("pass") is False:
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
        vocab_stops = _report_count(vocab.get("stops")) if isinstance(vocab, dict) else 0
        if (_report_count(residue.get("errors")) > 0
                or _report_count(residue.get("candidates")) > 0
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
                mode: str = "translation", no_analysis: bool = False,
                full: bool = False) -> int:
    started = time.time()
    outcome: dict = {"cid": chapter, "bid": block}
    rc = _cmd_prepare_impl(work, chapter, block, mode, outcome, no_analysis, full)
    _record_stage(work, "prepare", outcome["cid"], outcome["bid"], started, rc, mode=mode)
    return rc


def _cmd_prepare_impl(work: Path, chapter: str | None, block: str | None,
                      mode: str, outcome: dict, no_analysis: bool = False,
                      full: bool = False) -> int:
    if not work.is_dir():
        print(f"[prepare] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 排他ロック（共有ロックに一元化。解放は finally で必ず行う）
    ok, lock_reason = _acquire_lock_reason(work, "prepare")
    if not ok:
        print(f"[prepare] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        return 3
    try:
        return _cmd_prepare_locked(work, chapter, block, mode, outcome, no_analysis, full)
    finally:
        _release_lock(work)


def _cmd_prepare_locked(work: Path, chapter: str | None, block: str | None,
                        mode: str, outcome: dict, no_analysis: bool = False,
                        full: bool = False) -> int:
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

    # 残渣の事前警告（P0-3）: 分析を書き始める前に、既存 analysis の残渣に気づける
    # （stderr に1行だけ。止めはしない。保存前検査は analyze-commit 側が行う）。
    if mode == "analysis":
        warn = _analysis_residue_warning(work)
        if warn:
            print(warn, file=sys.stderr)

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

    # コンテキスト生成（--full は compact 解除。分析モードは full 固定:
    # build_context の CLI と同じ決定式。P1-4）
    work_dir_obj = work
    (work_dir_obj / "translation_input").mkdir(parents=True, exist_ok=True)
    from build_context import _stage_impl as bc_stage
    context_style = "full" if (full or mode == "analysis") else "compact"
    bc_stage(work, cid, bid, mode, context_style)

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


# ---------------------------------------------------------------------------
# draft-append サブコマンド（ドラフトの冪等分割書き込み。P2-4）
# ---------------------------------------------------------------------------
#
# 長いドラフトの write_file がタイムアウトしても、範囲ごとに分けて送り直せる
# ようにする。単純な追記だと再送で同じ範囲が二重に入るため、受領は
# 「依頼ID（--request-id）＋範囲＋内容ハッシュ」で冪等に行い、受領済みの範囲は
# output/_work/draft_parts/<ch>_<b>.json に記録する（依頼ID・範囲・ハッシュ・
# 本文。本文は完成時の連結に使う）。範囲の重複・欠落・範囲外は拒否し、
# 全範囲が揃ったときだけ、従来と同じ場所・形式
# （output/_work/<ch>_<b><suffix>_numbered.md）の完成ドラフトへ切り替える。
# 既存のドラフト配置（prepare→手書き→commit）は変更しない。

def _draft_parts_record_path(work: Path, cid: str, bid: str) -> Path:
    """受領済み部分ドラフトの記録先（output/_work/draft_parts/<ch>_<b>.json）。"""
    return work / "output" / "_work" / "draft_parts" / f"{cid}_{bid}.json"


def _load_draft_parts_record(work: Path, cid: str, bid: str) -> dict | None:
    """記録を読む（無い・読めない・壊れている場合は None）。"""
    p = _draft_parts_record_path(work, cid, bid)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _save_draft_parts_record(work: Path, cid: str, bid: str, record: dict) -> None:
    """記録を原子的に保存する。"""
    atomic_write_text(_draft_parts_record_path(work, cid, bid),
                      json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def _parse_part_markers(text: str) -> tuple[list[int], list[str]]:
    """部分ドラフトの [Pn] マーカーを出現順に読む。

    戻り値: (段落番号の列（重複も含む）, 形式不正のマーカー一覧)。
    行頭の [Pn] だけを対象にする（align.parse_numbered と同じ規則）。
    """
    nums: list[int] = []
    bad: list[str] = []
    for m in re.finditer(r"(?m)^\[P(\d+)([^\]]*)\]", text):
        if m.group(2):
            bad.append(m.group(0))
        else:
            nums.append(int(m.group(1)))
    return nums, bad


def _para_range_str(a: int, b: int) -> str:
    """段落範囲の表示（P4 / P4-P6）。"""
    return f"P{a}" if a == b else f"P{a}-P{b}"


def _para_ranges_str(ranges: list[tuple[int, int]]) -> str:
    """段落範囲の列の表示（P1-P3・P5）。"""
    return "・".join(_para_range_str(a, b) for a, b in ranges)


def _draft_parts_coverage(record: dict) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """記録から（受領済み範囲, 未受領範囲）を求める。範囲は (start, end) の列。"""
    rng = record.get("block_range") or []
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
        return [], []
    try:
        bs, be = int(rng[0]), int(rng[1])
    except (TypeError, ValueError):
        return [], []
    parts: list[tuple[int, int]] = []
    for p in record.get("parts") or []:
        if not isinstance(p, dict):
            continue
        try:
            parts.append((int(p.get("start")), int(p.get("end"))))
        except (TypeError, ValueError):
            continue
    parts.sort()
    covered: list[tuple[int, int]] = []
    for s, e in parts:
        if covered and s <= covered[-1][1] + 1:
            covered[-1] = (covered[-1][0], max(covered[-1][1], e))
        else:
            covered.append((s, e))
    missing: list[tuple[int, int]] = []
    expected = bs
    for s, e in covered:
        if s > expected:
            missing.append((expected, s - 1))
        expected = max(expected, e + 1)
    if expected <= be:
        missing.append((expected, be))
    return covered, missing


def _draft_parts_complete(record: dict) -> bool:
    """受領済み範囲がブロック全体（block_range）を連続して覆っているか。"""
    rng = record.get("block_range") or []
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
        return False
    try:
        bs, be = int(rng[0]), int(rng[1])
    except (TypeError, ValueError):
        return False
    covered, missing = _draft_parts_coverage(record)
    return (not missing and bool(covered)
            and covered[0][0] == bs and covered[-1][1] == be)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_para_arg(value: str) -> int | None:
    """`P12` / `12` 形式の段落番号を読む（不正なら None）。"""
    m = re.fullmatch(r"[Pp]?(\d+)", (value or "").strip())
    return int(m.group(1)) if m else None


def cmd_draft_append(work: Path, chapter: str, block: str, start: str, end: str,
                     request_id: str, file: str | None = None) -> int:
    """部分ドラフトを冪等に受け付ける。成功/失敗を timeline.jsonl に記録する。"""
    started = time.time()
    rc = _cmd_draft_append_impl(work, chapter, block, start, end, request_id, file)
    if work.is_dir():
        _record_stage(work, "draft-append", chapter or None, block or None, started, rc,
                      reason="success" if rc == 0 else "other")
    return rc


def _cmd_draft_append_impl(work: Path, chapter: str, block: str, start_s: str,
                           end_s: str, request_id: str, file: str | None) -> int:
    if not work.is_dir():
        print(f"[draft-append] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1
    request_id = (request_id or "").strip()
    if not request_id:
        print("[draft-append] エラー: --request-id が空です（再送の冪等キーに使います）",
              file=sys.stderr)
        return 1

    # 排他ロック（共有ロックに一元化。解放は finally で必ず行う）
    ok, lock_reason = _acquire_lock_reason(work, "draft-append")
    if not ok:
        print(f"[draft-append] エラー: {lock_reason}（終了コード 3）", file=sys.stderr)
        return 3
    try:
        return _cmd_draft_append_locked(work, chapter, block, start_s, end_s,
                                        request_id, file)
    finally:
        _release_lock(work)


def _cmd_draft_append_locked(work: Path, chapter: str, block: str, start_s: str,
                             end_s: str, request_id: str, file: str | None) -> int:
    # chapters_meta とブロックの範囲
    meta_p = work / "chapters_meta.toml"
    if not meta_p.is_file():
        meta_p = work / "chapters_meta.json"
    if not meta_p.is_file():
        print(f"[draft-append] エラー: chapters_meta がありません: {work}", file=sys.stderr)
        return 1
    try:
        meta = load_chapters_meta(work)
    except Exception as e:
        print(f"[draft-append] エラー: {e}", file=sys.stderr)
        return 1
    b = block_meta(meta, f"{chapter}/{block}")
    if b is None:
        print(f"[draft-append] エラー: {chapter}/{block} は chapters_meta に存在しません",
              file=sys.stderr)
        return 1
    rng = b.get("paras") or []
    if not (isinstance(rng, (list, tuple)) and len(rng) == 2):
        print(f"[draft-append] エラー: {chapter}/{block} の paras 範囲がありません",
              file=sys.stderr)
        return 1
    try:
        bs, be = int(rng[0]), int(rng[1])
    except (TypeError, ValueError):
        print(f"[draft-append] エラー: {chapter}/{block} の paras 範囲が不正です: {rng}",
              file=sys.stderr)
        return 1

    # 指定範囲（P12 / 12 の両形式）
    start = _parse_para_arg(start_s)
    end = _parse_para_arg(end_s)
    if start is None or end is None or start < 1 or end < 1:
        print(f"[draft-append] エラー: 範囲の指定が不正です"
              f"（--start {start_s} --end {end_s}。例: P1）", file=sys.stderr)
        return 1
    if start > end:
        print(f"[draft-append] エラー: 範囲が逆転しています（P{start} > P{end}）",
              file=sys.stderr)
        return 1
    if start < bs or end > be:
        print(f"[draft-append] エラー: 範囲 {_para_range_str(start, end)} はブロック "
              f"{chapter}/{block} の範囲 {_para_range_str(bs, be)} の外です（範囲外）",
              file=sys.stderr)
        return 1

    # 内容（--file または標準入力）
    if file:
        try:
            raw = Path(file).read_bytes()
        except OSError as e:
            print(f"[draft-append] エラー: ファイルを読めません: {file}: {e}", file=sys.stderr)
            return 1
    else:
        if sys.stdin.isatty():
            print("[draft-append] エラー: 内容がありません（--file か標準入力を使用してください）",
                  file=sys.stderr)
            return 1
        try:
            if hasattr(sys.stdin, "buffer"):
                raw = sys.stdin.buffer.read()
            else:
                raw = sys.stdin.read().encode("utf-8")
        except (OSError, ValueError) as e:
            print(f"[draft-append] エラー: 標準入力を読めません: {e}", file=sys.stderr)
            return 1
    try:
        text = raw.decode("utf-8-sig")  # BOM 付きでも先頭マーカーを読めるように
    except UnicodeDecodeError:
        print("[draft-append] エラー: 内容を UTF-8 として読めません", file=sys.stderr)
        return 1
    # 改行は LF に正規化して、連結結果を環境によらず決定的にする
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        print("[draft-append] エラー: 内容が空です", file=sys.stderr)
        return 1
    digest = sha12(raw)

    # 内容のマーカー検証: 指定範囲と一致すること（欠落・重複・範囲外を拒否）
    nums, bad = _parse_part_markers(text)
    if bad:
        print(f"[draft-append] エラー: マーカーの形式が不正です: {' '.join(bad[:3])}",
              file=sys.stderr)
        return 1
    if not nums:
        print("[draft-append] エラー: [Pn] マーカーがありません"
              "（部分ドラフトは番号付きで書いてください）", file=sys.stderr)
        return 1
    wanted = list(range(start, end + 1))
    dup = sorted({n for n in nums if nums.count(n) > 1})
    if dup:
        print(f"[draft-append] エラー: 内容のマーカーが重複しています: "
              f"{_para_ranges_str([(n, n) for n in dup])}（重複）", file=sys.stderr)
        return 1
    extra = sorted(set(nums) - set(wanted))
    if extra:
        print(f"[draft-append] エラー: 内容のマーカー {_para_ranges_str([(n, n) for n in extra])} "
              f"は指定範囲 {_para_range_str(start, end)} の外です（範囲外）", file=sys.stderr)
        return 1
    missing_in = sorted(set(wanted) - set(nums))
    if missing_in:
        print(f"[draft-append] エラー: 内容に "
              f"{_para_ranges_str([(n, n) for n in missing_in])} がありません（欠落）",
              file=sys.stderr)
        return 1

    # 受領記録（依頼ID で冪等に）
    record = _load_draft_parts_record(work, chapter, block)
    if record is None:
        # 既存のドラフト配置（prepare→手書き→commit）を壊さない: 無関係のドラフトが
        # 置かれている状態では新規の受領を始めない（完成時の上書きを避ける）。
        draft_p = _draft_path(work, chapter, block)
        if draft_p.is_file():
            print(f"[draft-append] エラー: 既存のドラフトがあります: {draft_p}。"
                  "上書きを避けるため受領を開始しません（退避か削除のうえ再実行してください）",
                  file=sys.stderr)
            return 1
        record = {
            "chapter": chapter, "block": block,
            "block_range": [bs, be],
            "complete": False,
            "parts": [],
        }
    else:
        rng2 = record.get("block_range") or []
        try:
            same_range = (isinstance(rng2, (list, tuple)) and len(rng2) == 2
                          and int(rng2[0]) == bs and int(rng2[1]) == be)
        except (TypeError, ValueError):
            same_range = False
        if not same_range:
            print(f"[draft-append] エラー: ブロック範囲が記録時と違います"
                  f"（記録 {rng2} / 現在 {_para_range_str(bs, be)}）。"
                  f"作り直すには記録を削除してください: "
                  f"{_draft_parts_record_path(work, chapter, block)}", file=sys.stderr)
            return 1

    parts: list[dict] = [p for p in (record.get("parts") or []) if isinstance(p, dict)]
    existing = next((p for p in parts if str(p.get("request_id")) == request_id), None)
    resend = False
    if existing is not None:
        # 同一依頼IDの再送: 範囲と内容ハッシュが一致するときだけ、記録の更新のみ行う
        try:
            same = (int(existing.get("start")) == start and int(existing.get("end")) == end
                    and str(existing.get("hash")) == digest)
        except (TypeError, ValueError):
            same = False
        if not same:
            print(f"[draft-append] エラー: 依頼ID {request_id} は受領済みですが内容が一致しません"
                  f"（受領 {existing.get('start')}-{existing.get('end')} hash {existing.get('hash')}"
                  f" / 今回 {start}-{end} hash {digest}）。"
                  "再送は同一の範囲・内容のときだけ受理します", file=sys.stderr)
            return 1
        existing["resends"] = int(existing.get("resends", 0) or 0) + 1
        existing["last_seen_at"] = _now_iso()
        resend = True
    else:
        if record.get("complete") is True:
            print(f"[draft-append] エラー: {chapter}/{block} のドラフトは完成済みです"
                  "（全範囲を受領済み）。再送は受領時の --request-id と同じIDを使ってください",
                  file=sys.stderr)
            return 1
        covered, _missing = _draft_parts_coverage(record)
        next_expected = bs if not covered else covered[-1][1] + 1
        overlap = next((c for c in covered if not (end < c[0] or start > c[1])), None)
        if overlap is not None:
            print(f"[draft-append] エラー: 範囲 {_para_range_str(start, end)} は受領済み範囲 "
                  f"{_para_range_str(*overlap)} と重複します"
                  "（重複。再送は同じ --request-id を使ってください）", file=sys.stderr)
            return 1
        if start > next_expected:
            print(f"[draft-append] エラー: 範囲 {_para_range_str(start, end)} の前に欠落があります"
                  f"（未受領 {_para_range_str(next_expected, start - 1)}。"
                  "先にその範囲を送ってください）", file=sys.stderr)
            return 1
        parts.append({
            "request_id": request_id,
            "start": start, "end": end,
            "hash": digest,
            "text": text,
            "resends": 0,
            "received_at": _now_iso(),
        })
        parts.sort(key=lambda p: int(p.get("start", 0)))
        record["parts"] = parts

    _save_draft_parts_record(work, chapter, block, record)

    if resend:
        print(f"[draft-append] 再送を受理（記録のみ。範囲 {_para_range_str(start, end)} は"
              f"重複しません。再送回数 {existing.get('resends')}）")
    else:
        covered, missing = _draft_parts_coverage(record)
        got_n = sum(e - s + 1 for s, e in covered)
        print(f"[draft-append] 受領: {_para_range_str(start, end)}"
              f"（{len(record['parts'])}範囲・{got_n}/{be - bs + 1}段落。"
              f"残り {_para_ranges_str(missing) or 'なし'}）")

    # 全範囲が揃ったら、従来と同じ場所・形式のドラフトへ切り替える
    if not record.get("complete") and _draft_parts_complete(record):
        return _finalize_draft_parts(work, chapter, block, record)
    return 0


def _finalize_draft_parts(work: Path, cid: str, bid: str, record: dict) -> int:
    """受領済みの部分ドラフトを連結し、従来のドラフト配置へ切り替える。"""
    parts = sorted((p for p in record.get("parts") or [] if isinstance(p, dict)),
                   key=lambda p: int(p.get("start", 0)))
    texts = [str(p.get("text") or "") for p in parts]
    assembled = "\n\n".join(t.strip("\n") for t in texts) + "\n"

    # 連結結果の自己検証（受領時の検証をすり抜けた場合の防波堤）
    nums, bad = _parse_part_markers(assembled)
    rng = record.get("block_range") or []
    try:
        bs, be = int(rng[0]), int(rng[1])
    except (TypeError, ValueError, IndexError):
        bs, be = 0, -1
    if bad or sorted(nums) != list(range(bs, be + 1)):
        print("[draft-append] エラー: 連結したドラフトのマーカーが範囲と一致しません"
              "（受領済みの範囲は保持しています）", file=sys.stderr)
        return 1

    draft_p = _draft_path(work, cid, bid)
    if draft_p.exists():
        # 手書きなど既存のドラフトを上書きしない
        print(f"[draft-append] エラー: 完成予定のドラフト配置先に既存ファイルがあります: {draft_p}。"
              "退避か削除のうえ再実行してください（受領済みの範囲は保持されています）",
              file=sys.stderr)
        return 1
    atomic_write_text(draft_p, assembled)
    record["complete"] = True
    record["completed_at"] = _now_iso()
    try:
        record["draft_path"] = draft_p.relative_to(work).as_posix()
    except ValueError:
        record["draft_path"] = str(draft_p)
    record["draft_hash"] = sha12(assembled.encode("utf-8"))
    _save_draft_parts_record(work, cid, bid, record)
    print(f"[draft-append] 完成: ドラフトを配置しました: {draft_p}（commit で確定できます）")
    return 0


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
    tee_token = None  # B1b: stdout/stderr のログ複製（finally で必ず止める）
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

        # #73: 検出の明細（語・段落）を配列で残す節。失敗して途中で止まった場合も
        # 節が欠けないよう、先に空の形で作って以降の工程で更新する。
        report = {"commit_id": draft_hash, "snapshot": str(snap_draft),
                  "chapter": cid, "block": bid, "revise": bool(revise), "stages": {
                      "align": {"fatal": [], "confirm_unacked": []},
                      "qa": {"pass": None, "checks": {}, "failures": []},
                      "residue": {"errors": [], "candidates": []},
                  }}
        # #73(B1b): 以降の stdout/stderr 明細をスナップショット配下の stdout.log にも
        # 残す（子エージェントの要約で消えても後から追える）。失敗しても本処理は止めない。
        tee_token = _start_output_tee(snap_dir / "stdout.log")

        # 2) align 検査（会話頭・欠落・重複マーカー）と未裁定ゲート（S3）
        # 旧 acks.jsonl は一回きり取り込み、裁定記録（rulings・全キー一致）で照合する。
        from align import check_block, migrate_legacy_acks
        migrate_legacy_acks(work)
        findings = check_block(work, cid, bid)
        fatals = [f for f in findings if f.severity == "fatal"]
        # 裁定記録で抑制されなかった confirm（文字数比等）は未裁定として確定を止める
        unacked_confirms = [f for f in findings if f.severity == "confirm"]
        report["stages"]["align"]["fatal"] = _align_finding_items(fatals)
        report["stages"]["align"]["confirm_unacked"] = _align_finding_items(unacked_confirms)

        if fatals:
            for f in fatals:
                print(f"[commit] fatal: P{f.para or '?'} {f.kind}: {f.detail}")
            det = _fmt_items([(f"P{f.para}" if f.para else "全体") + f" {f.kind}"
                              for f in fatals])
            _write_commit_report(snap_dir, report)
            print(f"[commit] 中止。align fatal {len(fatals)} 件: {det}。"
                  f"次の一手: 該当段落を原文と突き合わせてドラフト（{snap_draft}）を"
                  f"修正し、commit を再実行してください", file=sys.stderr)
            out["reason"] = "align"
            return 1

        if unacked_confirms:
            for f in unacked_confirms:
                print(f"[commit] confirm(未裁定): P{f.para or '?'} {f.kind}: {f.detail}")
            det = _fmt_items([(f"P{f.para}" if f.para else "全体") + f" {f.kind}"
                              for f in unacked_confirms])
            print(f"[commit] 失敗: confirm {len(unacked_confirms)} 件が未裁定です: {det}。"
                  f"次の一手: 本文を直して再実行するか、問題ない箇所は裁定記録で"
                  f"通過させてください: python3 scripts/align.py ack <work> --block {bid}"
                  f" --para <P番号> --reason \"確認理由\"（ack はドラフトのハッシュに"
                  f"紐づき、本文を変えると無効になります）", file=sys.stderr)
            _write_commit_report(snap_dir, report)
            out["reason"] = "confirm_unacked"
            return 1

        print(f"[commit] align: PASS (fatal={len(fatals)}, confirm(未裁定)={len(unacked_confirms)})")

        # 3) QA（非JIS・段落整合など）。検査対象はスナップショットと同じバイト列。
        from qa_translation import run_qa
        qa_report, items = run_qa(work, cid, bid, snap_draft)
        qa_stage = report["stages"]["qa"]
        qa_stage["pass"] = qa_report["pass"]
        qa_stage["checks"] = {k: v["pass"] for k, v in qa_report["checks"].items()}
        qa_stage["failures"] = _qa_failure_items(qa_report["checks"])

        if not qa_report["pass"]:
            fail_briefs = []
            for k, v in qa_report["checks"].items():
                if not v["pass"]:
                    print(f"[commit] QA FAIL: {k}: {v}")
                    fail_briefs.append(f"{k}{_qa_brief(v)}")
            _write_commit_report(snap_dir, report)
            print(f"[commit] 中止。QA 不合格 {len(fail_briefs)} 件: "
                  f"{' / '.join(fail_briefs)}。次の一手: ドラフトを修正して再実行してください。"
                  f"事前確認は python3 scripts/qa_translation.py {work}"
                  f" --chapter {cid} --block {bid}", file=sys.stderr)
            out["reason"] = "qa"
            return 1

        print(f"[commit] QA: PASS")

        # 3.5) 旧形式ルビ 親{読み} の警告（G11/T20）。VFM {親|読み} ではないため
        #      台帳にも review の初出検査にも載らない。自動変換はしない（語中ルビの
        #      判定が要るため）。確定は止めない（警告のみ）。
        if dst == "ja":
            try:
                from ledger import find_old_style_ruby
                old_ruby = find_old_style_ruby("\n".join(t for _, t in items))
            except Exception:
                old_ruby = []
            if old_ruby:
                forms = "、".join(e["form"] for e in old_ruby[:5])
                if len(old_ruby) > 5:
                    forms += f" ほか{len(old_ruby) - 5}件"
                print(f"[commit] 警告: 旧形式のルビが {len(old_ruby)} 件あります（{forms}）。"
                      f"{{訳語|よみ}} に変換してください"
                      f"（旧形式は台帳・review の初出検査に載りません）")
                if isinstance(report.get("stages", {}).get("qa"), dict):
                    report["stages"]["qa"]["old_style_ruby"] = len(old_ruby)

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
            report["stages"]["residue"]["candidates"] = _residue_finding_items(residue_candidates)
            report["stages"]["residue"]["errors"] = _residue_finding_items(residue_errors)

            if residue_errors:
                for f in residue_errors:
                    sf = f" → {f.safe_fix}" if f.safe_fix else ""
                    ctx = f.context[:40]
                    print(f"[commit] 残渣: 「{f.value}」{sf} (L{f.line}: {ctx})")
                det = _fmt_items([f.value for f in residue_errors])
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。残渣エラー {len(residue_errors)} 件: {det}。"
                      f"次の一手: 各行の指摘箇所（L 行番号）を日本語の訳語へ直して"
                      f"再実行してください", file=sys.stderr)
                out["reason"] = "residue"
                return 1

            if residue_candidates:
                for f in residue_candidates:
                    print(f"[commit] 残渣(候補): 「{f.value}」→ {f.safe_fix} (L{f.line})")
                # 候補にも safe_fix があるものは修正提案に過ぎないが、放置したまま
                # 確定するのはレビュー完了条件「既知パターンの機械検査の未解決エラー
                # 0件（emit 時）」に反する。候補が残る場合は確定を止め、修正か
                # house_style.toml の known_terms 登録を促す。
                det = _fmt_items([f.value for f in residue_candidates])
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。残渣候補 {len(residue_candidates)} 件: {det}。"
                      f"次の一手: 各行の修正案（→ の訳語）へ直して再実行するか、"
                      f"一般語として許容するなら house_style.toml の residue.known_terms へ"
                      f"登録してください", file=sys.stderr)
                out["reason"] = "residue"
                return 1

            # 統合辞書（作品 terms / house_style / 拡張 vocab.toml）による検査（P0-3 / #6）。
            # fixed: 違反は停止。candidates: 残存は停止（裁定記録 `vocab:<src>` で通過。
            # 全キー一致のみ有効）。ja_ok は check_draft が除外する。
            vocab = load_vocab(work)
            v_findings = vocab_check_draft(
                vocab, items, _block_src_text(work, f"{cid}/{bid}", meta),
                known_terms=known_terms,
                src_items=_block_src_items(work, f"{cid}/{bid}", meta))
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
                "stops": _vocab_stop_items(v_stops),
            }
            if v_stops:
                for vf in v_stops:
                    where = f"P{vf.paragraph:03d}" if vf.paragraph else "ブロック全体"
                    print(f"[commit] 残渣({vf.kind}): {vf.detail} ({where})")
                det = _fmt_items([
                    f"{vf.form}({vf.kind})"
                    + (f" P{vf.paragraph:03d}" if vf.paragraph else "")
                    for vf in v_stops])
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。辞書の残渣 {len(v_stops)} 件: {det}。"
                      f"登録済みの原語を訳文に使うと止まります（規定訳は terms.toml の"
                      f" term_ja。house_style / 拡張語彙も同様）。"
                      f"次の一手: fixed は本文を規定訳へ修正、candidates は本文修正か"
                      f"裁定記録（rulings.py、dict_version={vocab.dict_version()}）で"
                      f"通せます", file=sys.stderr)
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
            report["stages"]["residue"]["errors"] = _residue_finding_items(ja_residue)
            if ja_residue:
                for f in ja_residue[:20]:
                    print(f"[commit] 残渣: {f}")
                det = _fmt_items([str(f.value) for f in ja_residue])
                _write_commit_report(snap_dir, report)
                print(f"[commit] 中止。残渣 {len(ja_residue)} 件: {det}。"
                      f"次の一手: 該当箇所を中国語の訳文へ直して再実行してください",
                      file=sys.stderr)
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

        # 7) 台帳（ブロック横断状態の外部化。レビュー提案1）: 当該ブロックの確定訳文
        #    だけを走査し、ルビ初出と用語初出を _work/ledger.toml へ差分追記する
        #    （冪等）。既知の制限は ledger.py 冒頭のコメントを参照（rename_terms で
        #    親文字が変わっても台帳の term は古いまま／revise でルビが消えても履歴
        #    として残す）。失敗しても確定（emit 済み）は壊さない。
        try:
            from ledger import update_from_confirmed
            added = update_from_confirmed(work, f"{cid}/{bid}", items)
            if added["ruby"] or added["terms"]:
                print(f"[commit] 台帳: ルビ +{added['ruby']}件 / "
                      f"用語初出 +{added['terms']}件（_work/ledger.toml）")
        except Exception as e:
            print(f"[commit] 警告: 台帳（_work/ledger.toml）の更新に失敗しました: {e}",
                  file=sys.stderr)

        report["hashes"] = {k: rec[k] for k in
                            ("src_hash", "analysis_src_hash", "config_hash",
                             "draft_hash", "final_hash")}

        elapsed = time.time() - started
        n_residue = 0
        r_stage = report["stages"].get("residue", {})
        if isinstance(r_stage, dict):
            key = "errors" if (src == "ja" and dst == "zh") else "candidates"
            n_residue = _report_count(r_stage.get(key))

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
    finally:
        # B1b: ログ複製を止め、stdout/stderr を元へ戻す（例外時も必ず）。
        _stop_output_tee(tee_token)


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


# ---------------------------------------------------------------------------
# commit 失敗明細の可観測性（#73: report.json の明細配列 / #74: 中止メッセージ）
# ---------------------------------------------------------------------------


def _align_finding_items(findings: list) -> list[dict]:
    """align の fatal / confirm を report.json に残す明細（段落付き）へ変換する（#73）。"""
    return [{"kind": f.kind, "paragraph": f.para, "detail": f.detail}
            for f in findings]


def _residue_finding_items(findings: list) -> list[dict]:
    """残渣検査の検出を report.json に残す明細（語・行・修正案）へ変換する（#73）。"""
    return [{"value": f.value, "rule": f.rule, "line": f.line,
             "context": f.context[:80], "fix": f.safe_fix or ""}
            for f in findings]


def _vocab_stop_items(findings: list) -> list[dict]:
    """辞書検査（vocab）で停止した検出語の明細へ変換する（#73）。"""
    return [{"value": f.form, "kind": f.kind, "paragraph": f.paragraph,
             "detail": f.detail} for f in findings]


def _qa_failure_items(checks: dict) -> list[dict]:
    """QA の不合格チェックを、検出語（found 配列）付きの明細へ変換する（#73）。"""
    items: list[dict] = []
    for k, v in checks.items():
        if v.get("pass"):
            continue
        item: dict = {"check": k}
        found = v.get("found")
        if isinstance(found, list):
            item["found"] = found
        others = {kk: vv for kk, vv in v.items() if kk not in ("pass", "found")}
        if others:
            item["detail"] = others
        items.append(item)
    return items


def _fmt_items(values: list[str], limit: int = 12) -> str:
    """検出語・段落の一覧を「「A」、「B」ほかN件」の形にする（#74）。"""
    shown = "、".join(f"「{v}」" for v in values[:limit])
    if len(values) > limit:
        shown += f" ほか{len(values) - limit}件"
    return shown


def _qa_brief(check: dict) -> str:
    """QA 不合格チェックの検出語要約「（「A」、「B」…）」。found が無ければ空。"""
    found = check.get("found")
    if not isinstance(found, list) or not found:
        return ""
    words: list[str] = []
    for x in found:
        if isinstance(x, dict):
            for key in ("value", "char", "name_src", "form", "text"):
                if x.get(key):
                    words.append(str(x[key]))
                    break
            else:
                if x.get("para"):
                    words.append(f"P{x['para']}")
                elif "first" in x and "last" in x:
                    words.append(f"P{x['first']}〜P{x['last']}")
                else:
                    words.append(str(x))
        else:
            words.append(str(x))
    return f"（{_fmt_items(words)}）"


class _TeeStream:
    """write/flush を元のストリームとログファイルの両方へ流すラッパ（#73 B1b）。"""

    def __init__(self, stream, fh):
        self._stream = stream
        self._fh = fh

    def write(self, s):
        try:
            self._fh.write(s)
        except Exception:
            pass
        return self._stream.write(s)

    def flush(self):
        for obj in (self._fh, self._stream):
            try:
                obj.flush()
            except Exception:
                pass

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _start_output_tee(path: Path):
    """stdout/stderr の出力を path にも複製し始める（#73 B1b）。

    戻り値は _stop_output_tee に渡すトークン。開けなければ None（本処理は続行）。
    """
    try:
        fh = open(path, "a", encoding="utf-8")
    except OSError:
        return None
    orig_out, orig_err = sys.stdout, sys.stderr
    sys.stdout = _TeeStream(orig_out, fh)
    sys.stderr = _TeeStream(orig_err, fh)
    return (orig_out, orig_err, fh)


def _stop_output_tee(token) -> None:
    """_start_output_tee の複製を止め、ログファイルを閉じる（例外時も必ず呼ぶ）。"""
    if not token:
        return
    orig_out, orig_err, fh = token
    if isinstance(sys.stdout, _TeeStream):
        sys.stdout = orig_out
    if isinstance(sys.stderr, _TeeStream):
        sys.stderr = orig_err
    try:
        fh.flush()
        fh.close()
    except Exception:
        pass


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
    """各確定ブロックの report.json を _work/commit/ から収集する。

    ブロックごとに**最新タイムスタンプのスナップショット1件のみ**を読む（#75）。
    スナップショット名は <timestamp>_<cid>_<bid>（timestamp は %Y%m%d_%H%M%S_%f の
    固定幅）なので、名前の辞書順最大＝最新。過去の失敗スナップショットを拾い続けると
    解消済みの指摘が review に残り続けるため、最新以外は参照しない。
    """
    reports: list[dict] = []
    commit_dir = work / "output" / "_work" / "commit"
    if not commit_dir.is_dir():
        return reports
    for cid, bid in blocks:
        # 命名パターン: <timestamp>_<cid>_<bid> のサブディレクトリのうち最新のみ
        latest: Path | None = None
        for entry in commit_dir.iterdir():
            if not entry.is_dir():
                continue
            if not entry.name.endswith(f"_{cid}_{bid}"):
                continue
            if latest is None or entry.name > latest.name:
                latest = entry
        if latest is None:
            continue
        rp = latest / "report.json"
        if rp.is_file():
            try:
                data = json.loads(rp.read_text(encoding="utf-8"))
                data["_block"] = f"{cid}/{bid}"
                reports.append(data)
            except Exception:
                pass
    return reports


def _extract_machine_findings(reports: list[dict]) -> list[dict]:
    """report.json の stages から確認履歴（confirm / residue）を抽出。

    件数の欄は明細配列（新形式・#73）と数値（旧形式）の両方を読める。
    """
    findings: list[dict] = []
    for r in reports:
        stages = r.get("stages", {})
        align = stages.get("align", {})
        if isinstance(align, dict):
            confs = _report_count(align.get("confirm_unacked"))
            if confs > 0:
                findings.append({
                    "block": r.get("_block", ""),
                    "kind": "confirm未ack",
                    "count": confs,
                })
        residue = stages.get("residue", {})
        if isinstance(residue, dict):
            cands = _report_count(residue.get("candidates"))
            if cands > 0:
                findings.append({
                    "block": r.get("_block", ""),
                    "kind": "残渣候補",
                    "count": cands,
                })
            errs = _report_count(residue.get("errors"))
            if errs > 0:
                findings.append({
                    "block": r.get("_block", ""),
                    "kind": "残渣エラー",
                    "count": errs,
                })
    return findings


def _term_first_occurrences(work: Path, blocks: list[tuple[str, str]]) -> list[dict]:
    """ruby を持つ用語の初出（対象ブロックを順に原文走査して最初に現れる箇所）。

    戻り値の各要素は ``{term, term_src, ruby, block, para, text}``。対象ブロックの
    中に現れない用語は含めない（対象外ブロックの初出は出さない）。``para`` は
    初出段落の番号（段落列に無ければ None）、``text`` はその段落の原文。
    """
    try:
        terms = load_terms(work).get("terms") or []
    except Exception:
        return []
    ruby_terms: list[tuple[str, str, str]] = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        src = str(t.get("term_src") or "").strip()
        ruby = str(t.get("ruby") or "").strip()
        if not src or not ruby:
            continue
        name = str(t.get("term_ja") or "").strip() or src
        ruby_terms.append((name, src, ruby))
    if not ruby_terms:
        return []
    meta = load_chapters_meta(work)
    paras_cache: dict[str, dict] = {}
    block_cache: dict[str, dict | None] = {}
    text_cache: dict[str, str] = {}
    out: list[dict] = []
    for name, src, ruby in ruby_terms:
        hit: dict | None = None
        for cid, bid in blocks:
            key = f"{cid}/{bid}"
            if key not in block_cache:
                block_cache[key] = block_meta(meta, key)
            b = block_cache[key]
            if b is None:
                continue
            if key not in text_cache:
                text_cache[key] = _block_src_text(work, key, meta)
            if src not in text_cache[key]:
                continue
            para_n: int | None = None
            para_text = ""
            if cid not in paras_cache:
                try:
                    paras_cache[cid] = load_paras(work, cid)
                except Exception:
                    paras_cache[cid] = {}
            for p in block_paras(paras_cache[cid], b):
                pt = str(p.get("text") or "")
                if src in pt:
                    para_n = int(p.get("n", 0))
                    para_text = pt
                    break
            hit = {"term": name, "term_src": src, "ruby": ruby,
                   "block": key, "para": para_n, "text": para_text}
            break
        if hit is not None:
            out.append(hit)
    return out


def _find_risk_blocks(work: Path, blocks: list[tuple[str, str]]) -> list[dict]:
    """リスクの高い箇所を列挙:
    - arcs の at_block 開始点
    - address を持つ人物がいるブロック
    - ruby を持つ人物の初出ブロック
    - ruby を持つ用語の初出ブロック（用語ルビ。対象ブロック内のみ）
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

    # ruby を持つ用語の初出（用語ルビ）。対象ブロックを順に原文走査し、最初に
    # term_src が現れるブロックだけを出す（対象外ブロックの初出は出さない）
    for occ in _term_first_occurrences(work, blocks):
        risk_items.append({
            "block": occ["block"],
            "kind": f"{occ['term']} 初出（用語ルビ）",
            "detail": f"ruby={occ['ruby']}",
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
    """指定ブロックの段落から ratio の割合で決定的に選択する（seed は固定）。

    末尾に ruby を持つ用語の初出段落を ``kind="用語初出"`` として決定的に
    追記する（ランダム部は変えない。初出の確認を強制するための追記）。
    """
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
    # 強制追加: ruby を持つ用語の初出段落（決定的。既存の選定の後ろへ追記する）
    seen_forced: set[tuple[str, int]] = set()
    for occ in _term_first_occurrences(work, blocks):
        para_n = occ["para"]
        if para_n is None:
            continue
        key = (occ["block"], para_n)
        if key in seen_forced:
            continue
        seen_forced.add(key)
        samples.append({
            "block": occ["block"],
            "para": para_n,
            "text": occ["text"],
            "kind": "用語初出",
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
            kind = str(s.get("kind") or "")
            head = f"### {blk} P{pn}"
            if kind:
                head += f"（{kind}）"
            rev_lines.append(head)
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


# ---------------------------------------------------------------------------
# ledger-ono サブコマンド（ブロック横断状態の外部化。レビュー提案1）
# ---------------------------------------------------------------------------
#
# 使用済みオノマトペの申告を _work/ledger.toml の [[ono_used]] へ追記する。
# 本体（原文・訳文）にオノマトペの表が無いため自動抽出はせず、翻訳者が
# 「この表記はもう使った」と申告するための入口。同一 form+block+para は
# 重複させない（再申告は「既に記録済み」の1行を出して exit 0）。

def cmd_ledger_ono(work: Path, form: str, block: str,
                   paragraph: int | None = None, context: str = "") -> int:
    """オノマトペ使用の申告を台帳へ追記する（成功・失敗を timeline に1行記録）。"""
    started = time.time()
    rc = _cmd_ledger_ono_impl(work, form, block, paragraph, context)
    _record_stage(work, "ledger-ono", None, block, started, rc,
                  reason="success" if rc == 0 else "other")
    return rc


def _cmd_ledger_ono_impl(work: Path, form: str, block: str,
                         paragraph: int | None, context: str) -> int:
    if not work.is_dir():
        print(f"[ledger-ono] エラー: ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1
    if not str(form or "").strip():
        print("[ledger-ono] エラー: --form は空にできません", file=sys.stderr)
        return 1
    try:
        from ledger import declare_ono
        res = declare_ono(work, form, block, paragraph, context)
    except Exception as e:
        print(f"[ledger-ono] エラー: 台帳を更新できません: {e}", file=sys.stderr)
        return 1
    form_s = str(form).strip()
    block_s = str(block or "").strip()
    para_s = f" para={paragraph}" if paragraph is not None else ""
    if res["added"]:
        print(f"[ledger-ono] 記録しました: form={form_s} block={block_s}{para_s}"
              f"（オノマトペ使用 {res['count']}件）")
    else:
        print(f"[ledger-ono] 既に記録済みです（重複しません）: "
              f"form={form_s} block={block_s}{para_s}")
    return 0


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="saga-translator CLI")
    sub = ap.add_subparsers(dest="command", required=True)

    # status
    p_status = sub.add_parser("status", help="進捗確認")
    p_status.add_argument("work", help="作品ディレクトリ")

    # proposed
    p_prop = sub.add_parser("proposed", help="訳語変更の候補（proposed）の一覧・承認・却下")
    p_prop.add_argument("action", choices=("list", "approve", "reject"))
    p_prop.add_argument("work", help="作品ディレクトリ")
    p_prop.add_argument("--name", default=None, help="訳語（name_ja/term_ja または原語）")
    p_prop.add_argument("--key", default=None, help="承認・却下する欄（name_ja, role 等）")
    p_prop.add_argument("--reason", default="", help="判断理由（監査記録に残す）")

    # lint
    p_lint = sub.add_parser("lint", help="分析 TOML の日本語フィールド残渣の事前一覧（読み取り専用）")
    p_lint.add_argument("work", help="作品ディレクトリ")
    p_lint.add_argument("--delta", default=None, help="差分TOMLも検査する")

    # analyze-commit
    p_ac = sub.add_parser("analyze-commit", help="分析差分のコミット")
    p_ac.add_argument("work", help="作品ディレクトリ")
    p_ac.add_argument("--delta", required=True, help="差分TOMLファイル")
    p_ac.add_argument("--force-residue", action="store_true",
                      help="分析 TOML の中国語残渣を警告のうえで通す（block 記録に残す）")
    p_ac.add_argument("--allow-protected-overwrite", action="store_true",
                      dest="allow_protected_overwrite",
                      help="記述系キー（memo / role / appearance / relationships / note）の"
                           "上書きを許可し、_work/analysis_edits.jsonl に監査記録を残す"
                           "（name_ja / term_ja は対象外＝rename_terms の領分）")

    # prepare
    p_prep = sub.add_parser("prepare", help="翻訳コンテキスト準備")
    p_prep.add_argument("work", help="作品ディレクトリ")
    p_prep.add_argument("--chapter", default=None, help="章（例: ch001）")
    p_prep.add_argument("--block", default=None, help="ブロック（例: b2）")
    p_prep.add_argument("--mode", choices=["translation", "analysis"], default="translation",
                        help="コンテキストのモード（既定: translation）")
    p_prep.add_argument("--no-analysis", action="store_true",
                        help="未分析ブロックの翻訳を明示的に許可する（analysis_skipped として記録）")
    p_prep.add_argument("--full", action="store_true",
                        help="compact をやめて全件の人物表・用語集を出力する")

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

    # draft-append
    p_da = sub.add_parser("draft-append",
                          help="ドラフトの冪等分割書き込み（部分ドラフトの受領）")
    p_da.add_argument("work", help="作品ディレクトリ")
    p_da.add_argument("--chapter", required=True, help="章（例: ch001）")
    p_da.add_argument("--block", required=True, help="ブロック（例: b2）")
    p_da.add_argument("--start", required=True, help="開始段落（例: P1）")
    p_da.add_argument("--end", required=True, help="終了段落（例: P3）")
    p_da.add_argument("--request-id", required=True, dest="request_id",
                      help="依頼ID（冪等キー。再送は同じIDを使う）")
    p_da.add_argument("--file", default=None, help="部分ドラフトのファイル（省略時は標準入力）")

    # ledger-ono
    p_lo = sub.add_parser("ledger-ono",
                          help="使用済みオノマトペの申告（_work/ledger.toml へ追記）")
    p_lo.add_argument("work", help="作品ディレクトリ")
    p_lo.add_argument("--form", required=True, help="オノマトペの表記")
    p_lo.add_argument("--block", required=True, help="ブロック（例: ch001/b2）")
    p_lo.add_argument("--para", type=int, default=None, help="段落番号（任意）")
    p_lo.add_argument("--context", default="", help="注記（任意）")

    args = ap.parse_args()

    work = Path(args.work).resolve()

    if args.command == "status":
        raise SystemExit(cmd_status(work))
    elif args.command == "proposed":
        if args.action == "list":
            raise SystemExit(cmd_proposed_list(work))
        if not args.name or not args.key:
            print("エラー: approve/reject には --name と --key が必要です", file=sys.stderr)
            raise SystemExit(1)
        raise SystemExit(_apply_proposed(work, args.name, args.key,
                                         approve=(args.action == "approve"),
                                         reason=args.reason))
    elif args.command == "lint":
        raise SystemExit(cmd_lint(work, resolve_delta_path(work, args.delta) if args.delta else None))
    elif args.command == "analyze-commit":
        raise SystemExit(cmd_analyze_commit(work, resolve_delta_path(work, args.delta),
                                            force_residue=args.force_residue,
                                            allow_protected_overwrite=args.allow_protected_overwrite))
    elif args.command == "prepare":
        raise SystemExit(cmd_prepare(work, args.chapter, args.block, mode=args.mode,
                                     no_analysis=args.no_analysis, full=args.full))
    elif args.command == "review":
        raise SystemExit(cmd_review(work, wave=args.wave,
                                     sample_ratio=args.sample_ratio,
                                     json_output=args.json))
    elif args.command == "commit":
        raise SystemExit(cmd_commit(work, args.chapter, args.block,
                                     args.model, args.prompt_version,
                                     revise=args.revise,
                                     no_analysis=args.no_analysis))
    elif args.command == "draft-append":
        raise SystemExit(cmd_draft_append(work, args.chapter, args.block,
                                          args.start, args.end,
                                          args.request_id, args.file))
    elif args.command == "ledger-ono":
        raise SystemExit(cmd_ledger_ono(work, args.form, args.block,
                                        args.para, args.context))


if __name__ == "__main__":
    main()