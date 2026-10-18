"""翻訳/分析用コンテキストの組み立て。エージェントは生成されたファイルだけを読む。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    character_aliases, characters_toml_path, direction, find_unnormalized_images,
    load_chapters_meta, load_characters,
    load_config, load_json, load_paras, load_plot, load_structured, load_terms, load_titles,
    load_toml, save_chapters_meta, save_json, target_suffix, target_title, utf8, voice_status_of,
)


# prepare が出すコンテキストの冒頭に置く、段落をまたぐ台詞の引用閉じ規則（P0-1）。
# 文言は仕様で固定（改変禁止）。引用を開いたまま段落を終えると layout_lines が
# 段落の区切りを消して連結する（qa の merged_paragraphs が検出して commit を止める）。
QUOTE_RULE = "【段落の引用の閉じ方】段落をまたぐ台詞でも、その段落の終わりで 」 を閉じる。次の段落では 「 を開き直す。引用を開いたまま段落を終えないこと。"
QUOTE_RULE_EXAMPLE = "（例: 会話が2段落に続くとき、1段落目の終わりに 」 だけを置いて閉じる）"


def load_analysis(work: Path) -> tuple[dict, dict, dict]:
    return load_characters(work), load_terms(work), load_plot(work)


# voice_status の表示名（人物表に必ず出す。P0-4）
VOICE_STATUS_LABELS = {
    "confirmed": "確定",
    "unconfirmed": "未確認",
    "insufficient_evidence": "根拠不足",
    "no_speech_yet": "現時点で発話なし",
}


def fmt_characters(chars: dict) -> str:
    """翻訳コンテキスト用。声の欄を混ぜずに出す。旧 tone は speech_style が空のときだけ。

    aliases（別名・愛称）は空なら省略。声の状態（voice_status）は必ず出し、
    根拠（voice_note）があれば添える（P0-4）。
    """
    blocks: list[str] = []
    for c in chars.get("characters", []):
        name = c.get("name_ja") or c.get("name_src") or "(無名)"
        src = c.get("name_src") or ""
        head = f"### {name}" + (f"（{src}）" if src and src != name else "")
        lines = [head]
        def add(label: str, key: str) -> None:
            v = str(c.get(key) or "").strip()
            if v:
                lines.append(f"- {label}: {v}")
        aliases = character_aliases(c)
        if aliases:
            lines.append("- 別名: " + " / ".join(aliases))
        add("役割", "role")
        status = voice_status_of(c)
        voice_line = f"- 声: {VOICE_STATUS_LABELS.get(status, VOICE_STATUS_LABELS['unconfirmed'])}"
        note = str(c.get("voice_note") or "").strip()
        if note:
            voice_line += f"（根拠: {note}）"
        lines.append(voice_line)
        add("一人称", "first_person")
        add("二人称", "second_person")
        addr = c.get("address") or []
        if addr:
            bits = []
            for a in addr:
                tgt = a.get("target_ja") or a.get("target_src") or "?"
                bits.append(f"{tgt}→{a.get('form', '')}")
            lines.append("- 呼び方: " + " / ".join(bits))
        add("口調", "speech_style")
        if not str(c.get("speech_style") or "").strip() and str(c.get("tone") or "").strip():
            lines.append(f"- 語調(旧): {c.get('tone')}")
        add("語彙", "vocab")
        add("口癖", "catchphrase")
        add("外見", "appearance")
        add("関係", "relationships")
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    return "\n\n".join(blocks) if blocks else "(未登録)"


def fmt_arcs(chars: dict) -> str:
    """キャラの変化(arc)を翻訳コンテキストへ出す。節目だけ訳し分けに使う。"""
    lines = []
    for c in chars.get("characters", []):
        arcs = c.get("arcs") or []
        if not arcs:
            continue
        lines.append(f"### {c.get('name_ja') or c.get('name_src') or ''}")
        for a in arcs:
            start = a.get("at_block", "?")
            bits = []
            if a.get("speech_style"):
                bits.append(f"口調: {a['speech_style']}")
            if a.get("first_person"):
                bits.append(f"一人称: {a['first_person']}")
            if a.get("second_person"):
                bits.append(f"二人称: {a['second_person']}")
            if a.get("vocab"):
                bits.append(f"語彙: {a['vocab']}")
            if a.get("catchphrase"):
                bits.append(f"口癖: {a['catchphrase']}")
            if a.get("note"):
                bits.append(a["note"])
            if a.get("tone") and not a.get("speech_style"):
                bits.append(a["tone"])
            lines.append(f"- {start}〜: " + " / ".join(bits) if bits else f"- {start}〜")
    return "\n".join(l for l in lines if l) if lines else "(なし)"


def fmt_terms(terms: dict) -> str:
    rows = ["| 原語 | 訳語 | 読み | メモ |", "|---|---|---|---|"]
    for t in terms.get("terms", []):
        rows.append(f"| {t.get('term_src','')} | {t.get('term_ja','')} | {t.get('ruby','') or ''} | {t.get('memo','') or ''} |")
    return "\n".join(rows) if len(rows) > 2 else "(未登録)"


# ── あらすじの文字数予算（G20 / F4-A1 / F5-B1） ────────────────────────
# あらすじはブロックごとに累積し、実測では本文の3倍超（b10 で 12,159ch）に
# 達した。予算（文字数）を超えたら古い側を1行の要約に畳んでコンテキストを
# 抑える。要約は機械的な圧縮（件数＋冒頭の抜粋）で、内容は消さない。

#: 予算の既定値（config `[context].plot_budget_chars` / --plot-budget の既定）。
#: 0 で無制限（従来挙動）。
DEFAULT_PLOT_BUDGET = 4000

#: 1行要約に残す冒頭の文字数
PLOT_SUMMARY_HEAD = 80


def _condense_to_budget(texts: list[str], budget: int) -> tuple[list[str], int]:
    """あらすじ本文（古い順）を予算内に収める。落とした古い側は1行に畳む。

    新しい側から積み、予算を超える古い分を落とす（最新の1件は常に残す）。
    落とした分は「（以前の経過N件を1行に省略: 冒頭「…」）」の1行になる。
    budget<=0 は無制限（素通し）。戻り値: (表示行, 省略件数)。
    """
    if budget <= 0 or not texts:
        return list(texts), 0
    kept: list[str] = []
    total = 0
    for t in reversed(texts):
        cost = len(t) + 2  # "- " の分
        if kept and total + cost > budget:
            break
        kept.append(t)
        total += cost
    kept.reverse()
    dropped = len(texts) - len(kept)
    if dropped <= 0:
        return list(texts), 0
    head = str(texts[0]).strip().replace("\n", " ")
    if len(head) > PLOT_SUMMARY_HEAD:
        head = head[:PLOT_SUMMARY_HEAD] + "…"
    summary = f"（以前の経過{dropped}件を1行に省略: 冒頭「{head}」）"
    return [summary] + kept, dropped


def fmt_plot(plot: dict, tail: int = 20, budget: int = 0) -> str:
    lines = [e.get("text", "") for e in plot.get("entries", [])]
    if not lines:
        return "(未記録)"
    lines, _ = _condense_to_budget(lines[-tail:], budget)
    return "\n".join(f"- {l}" for l in lines)


def prev_tail_text(work: Path, chapters: list[dict], ci: int, bi: int, k: int) -> str:
    """前ブロック(なければ前章)の訳文末尾k段落。"""
    suf = target_suffix(direction(work)[1])
    for ci2 in range(ci, -1, -1):
        ch = chapters[ci2]
        blocks = ch["blocks"]
        for bi2 in range(len(blocks) - 1, -1, -1):
            if ci2 == ci and bi2 >= bi:
                continue
            path = work / "output" / ch["id"] / f"{blocks[bi2]['id']}{suf}.md"
            if path.exists():
                paras = [p.strip() for p in path.read_text(encoding="utf-8").split("\n\n") if p.strip()]
                return "\n\n".join(paras[-k:])
    return "(なし: 冒頭)"


def prev_tail_text_raw(work: Path, chapters: list[dict], ci: int, bi: int) -> str:
    """前ブロック(なければ前章)の訳文全文。絞り込み判定用。"""
    suf = target_suffix(direction(work)[1])
    for ci2 in range(ci, -1, -1):
        ch = chapters[ci2]
        blocks = ch["blocks"]
        for bi2 in range(len(blocks) - 1, -1, -1):
            if ci2 == ci and bi2 >= bi:
                continue
            path = work / "output" / ch["id"] / f"{blocks[bi2]['id']}{suf}.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
    return "(なし: 冒頭)"


def next_head_text(chapters: list[dict], ci: int, bi: int, k: int, chdir: Path) -> str:
    if bi + 1 < len(chapters[ci]["blocks"]):
        s, e = chapters[ci]["blocks"][bi + 1]["paras"]
        cid = chapters[ci]["id"]
        paras = load_structured(chdir / f"{cid}.paras")["paras"]
        return "\n\n".join(p["text"] for p in paras[s - 1:s - 1 + k])
    if ci + 1 < len(chapters):
        cid = chapters[ci + 1]["id"]
        paras = load_structured(chdir / f"{cid}.paras")["paras"]
        return "\n\n".join(p["text"] for p in paras[:k])
    return "(なし: 最終ブロック)"


# ── compact 用補助関数 ──────────────────────────────────────────

def _block_address(cid: str, bid: str) -> tuple[int, int]:
    """chNNN/bM → (章番号, ブロック番号)。ch001/b1 → (1, 1)"""
    import re
    m = re.match(r"ch(\d+)/b(\d+)", f"{cid}/{bid}")
    if not m:
        return (0, 0)
    return (int(m.group(1)), int(m.group(2)))


def _parse_at_block(at_block: str) -> tuple[int, int] | None:
    """chNNN/bM → (章, ブロック) or None"""
    import re
    m = re.match(r"ch(\d+)/b(\d+)", at_block)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    return None


# arcs が持ちうる声の欄（時点解決の対象。表示はこの順。人物ごとに独立）。
# fmt_arcs と同じ欄＋address（呼び方）。tone は旧欄（speech_style が無いときだけ出す）。
VOICE_ARC_FIELDS = ("first_person", "second_person", "address", "speech_style",
                    "vocab", "catchphrase", "tone")

_ARC_FIELD_LABELS = {
    "first_person": "一人称",
    "second_person": "二人称",
    "address": "呼び方",
    "speech_style": "口調",
    "vocab": "語彙",
    "catchphrase": "口癖",
    "tone": "語調(旧)",
}

# 対象ブロックの途中で起きた変化の注記（#7b。文言は仕様で固定）
MID_BLOCK_NOTE = "（このブロックの途中で変化）"


def _arc_field_value(arc: dict, field: str):
    """arc の声の欄の値。空（空文字・空リスト・空テーブル）は None 扱い。"""
    v = arc.get(field)
    if isinstance(v, str):
        return v if v.strip() else None
    if v is None:
        return None
    if isinstance(v, (list, dict)):
        return v if v else None
    return v


def _compact_arcs(
    chars: dict,
    cid: str, bid: str,
    include_target: bool = False,
) -> tuple[dict, list[str]]:
    """compact モード: 対象ブロック時点で有効な声の変化を、欄ごとに累積して出す。

    `at_block <= 対象`（include_target=False では `< 対象`）の各 arc を走査し、
    声の欄ごとに最新の値を採る。全体の最新1件に潰さない（b1 で一人称、b2 で
    口調が変われば、b3 には両方が効く。#7b）。対象ブロックの途中で起きた変化
    （at_block == 対象）も適用し、その欄に `（このブロックの途中で変化）` を付ける。

    戻り値は `{名前: [累積した1件の arc]}` と警告行。arc には声の欄のほか、
    欄ごとの出所 `field_sources`、途中変化の欄 `mid_block_fields`、注記
    `arc_notes` を持つ。include_target の既定 False は後方互換（対象より前だけ）。
    コンテキスト生成（_stage_impl）は True で呼び、対象ブロックの途中の変化も
    そのブロックに反映する。
    """
    target = _block_address(cid, bid)
    warn_lines: list[str] = []
    result: dict[str, list[dict]] = {}
    for c in chars.get("characters", []):
        if not isinstance(c, dict):
            continue
        arcs = c.get("arcs") or []
        if not isinstance(arcs, list):
            continue
        name = c.get("name_ja") or c.get("name_src") or ""
        applicable: list[tuple[tuple[int, int], str, dict]] = []
        for a in arcs:
            if not isinstance(a, dict):
                continue
            at_block = a.get("at_block", "")
            if not at_block:
                continue
            if at_block == "carry":
                # carry_analysis が書く引き継ぎの目印（G22 / CA-2）。作品開始時点の
                # 起点なので常に適用する。声の欄は基底側に畳み込み済みで、ここでは
                # note を「注記」として残す（従来は不正値として警告・スキップ）。
                applicable.append(((0, 0), at_block, a))
                continue
            parsed = _parse_at_block(at_block)
            if parsed is None:
                warn_lines.append(
                    f"# WARNING: 不正な at_block '{at_block}' "
                    f"(arc: {a.get('note', '')}) をスキップしました"
                )
                continue
            if parsed < target or (include_target and parsed == target):
                applicable.append((parsed, at_block, a))
        if not applicable:
            continue
        # 古い順に畳む。同じ欄は後の値で上書きされ、欄ごとの最新値だけが残る。
        applicable.sort(key=lambda x: x[0])
        merged: dict = {}
        sources: dict[str, str] = {}
        mid_fields: list[str] = []
        notes: list[tuple[str, str]] = []
        for parsed, at_block, a in applicable:
            for field in VOICE_ARC_FIELDS:
                v = _arc_field_value(a, field)
                if v is None:
                    continue
                merged[field] = v
                sources[field] = at_block
                if field in mid_fields:
                    mid_fields.remove(field)
                if include_target and parsed == target:
                    mid_fields.append(field)
            note = str(a.get("note") or "").strip()
            if note and (at_block, note) not in notes:
                notes.append((at_block, note))
        merged["field_sources"] = sources
        merged["mid_block_fields"] = mid_fields
        merged["arc_notes"] = notes
        result[name] = [merged]
    return result, warn_lines


def _fmt_arc_value(field: str, value) -> str:
    """累積 arc の欄の値を表示用の文字列にする（address は target→form を連結）。"""
    if field == "address" and isinstance(value, list):
        bits = []
        for a in value:
            if isinstance(a, dict):
                tgt = a.get("target_ja") or a.get("target_src") or "?"
                bits.append(f"{tgt}→{a.get('form', '')}")
        return " / ".join(bits)
    return str(value)


def fmt_arcs_resolved(resolved: dict) -> str:
    """時点解決済みの声の変化（_compact_arcs の累積結果）を整形する（#7b）。

    - 欄ごとの最新値だけを出す（全体の最新1件に潰さない）
    - 出所のブロックを（chNNN/bM〜）で添える
    - 対象ブロックの途中で変わった欄には（このブロックの途中で変化）を付ける
    - arc の note は「注記」行にまとめる
    """
    blocks_out: list[str] = []
    for name, arcs in resolved.items():
        merged = arcs[0] if arcs else {}
        lines = [f"### {name}"]
        for field in VOICE_ARC_FIELDS:
            if field == "tone" and "speech_style" in merged:
                continue  # 旧 tone は speech_style が無いときだけ（fmt_arcs と同じ）
            if field not in merged:
                continue
            val = _fmt_arc_value(field, merged[field])
            if not val:
                continue
            label = _ARC_FIELD_LABELS.get(field, field)
            if field in (merged.get("mid_block_fields") or []):
                lines.append(f"- {label}: {val}{MID_BLOCK_NOTE}")
            else:
                src = (merged.get("field_sources") or {}).get(field, "")
                lines.append(f"- {label}: {val}（{src}〜）")
        notes = merged.get("arc_notes") or []
        if notes:
            lines.append("- 注記: " + " / ".join(f"{b} {n}" for b, n in notes))
        if len(lines) > 1:
            blocks_out.append("\n".join(lines))
    return "\n\n".join(blocks_out) if blocks_out else "(なし)"


def _compact_characters(
    chars: dict,
    body_texts: list[str],
    prev_translation: str,
) -> list[dict]:
    """compact モード: 登場人物を絞り込む。"""
    core_chars = []
    candidates = []
    for c in chars.get("characters", []):
        name_src = c.get("name_src") or ""
        name_ja = c.get("name_ja") or ""
        ruby = c.get("ruby") or ""
        address_items = c.get("address") or []

        in_src = bool(name_src) and any(name_src in t for t in body_texts)
        in_src_ruby = bool(ruby) and any(ruby in t for t in body_texts)
        in_src_addr = any(
            a.get("target_src") and a["target_src"] in t
            for a in address_items
            for t in body_texts
        )

        in_prev = bool(name_ja) and name_ja in prev_translation
        in_prev_addr = any(
            a.get("target_ja") and a["target_ja"] in prev_translation
            for a in address_items
        )

        is_core = bool(c.get("core"))
        matched = in_src or in_src_ruby or in_src_addr or in_prev or in_prev_addr

        if is_core or matched:
            if is_core:
                core_chars.append(c)
            else:
                candidates.append(c)

    seen = set()
    result = []
    for c in core_chars + candidates:
        sid = id(c)
        if sid not in seen:
            seen.add(sid)
            result.append(c)
    return result


def _omitted_character_names(chars: dict, shown: list[dict]) -> list[str]:
    """compact で落ちた人物の表示名（提案 P1-4 の可視化。stderr に出す）。"""
    shown_ids = {id(c) for c in shown}
    names: list[str] = []
    for c in chars.get("characters", []):
        if not isinstance(c, dict) or id(c) in shown_ids:
            continue
        names.append(str(c.get("name_ja") or c.get("name_src") or "(無名)"))
    return names


def _compact_terms(
    terms: dict,
    body_texts: list[str],
    prev_translation: str,
) -> list[dict]:
    """compact モード: 用語を絞り込む（G21 / F4-A2 / F5-B2）。

    対象本文に原文形（term_src）が出現する語と core だけを載せる。従来の
    「前ブロック訳文に term_ja が出る語」も載せる条件（in_prev）は、前ブロック
    の訳文が全登録語を含むため用語集が本文と同量まで膨らむ原因だった
    （実測 b10 で 3,594ch）。prev_translation は互換のため受け取るが使わない。
    """
    result = []
    seen = set()
    for t in terms.get("terms", []):
        term_src = t.get("term_src") or ""
        is_core = bool(t.get("core"))

        in_src = bool(term_src) and any(term_src in bt for bt in body_texts)

        if is_core or in_src:
            tid = id(t)
            if tid not in seen:
                seen.add(tid)
                result.append(t)
    return result


def _compact_plot(
    plot: dict,
    cid: str, bid: str,
    default_tail: int = 20,
    budget: int = 0,
) -> tuple[list[str], bool]:
    """compact モード: 対象ブロックより前の plot entries のみ（予算で古い側を1行化）。"""
    target = _block_address(cid, bid)
    entries = plot.get("entries", [])
    if not entries:
        return [], False

    all_have_block = True
    filtered = []
    for e in entries:
        block = e.get("block")
        if block is None:
            all_have_block = False
            filtered.append(e)
        else:
            parsed = _parse_at_block(block)
            if parsed is None:
                all_have_block = False
                filtered.append(e)
            elif parsed < target:
                filtered.append(e)

    if not all_have_block:
        texts, _ = _condense_to_budget(
            [e.get("text", "") for e in entries[-default_tail:]], budget)
        return texts, False

    texts, _ = _condense_to_budget(
        [e.get("text", "") for e in filtered[-default_tail:]], budget)
    return texts, True


def _load_house_style_rules(work: Path) -> list[dict]:
    """work の _house と work 親の _house の house_style.toml から全 rule を読む。

    探索先は vocab.house_style_paths に共通化（#19。prepare と commit で同じ2箇所。
    従来はここが work.parent.parent、saga 側が work.parent で不一致だった）。
    """
    from vocab import house_style_paths
    rules = []
    for p in house_style_paths(work):
        try:
            data = load_toml(p)
            if data and isinstance(data, dict):
                for r in data.get("rules", []):
                    rules.append(r)
        except Exception:
            pass
    return rules


def _load_house_style_policy(work: Path) -> dict | None:
    """house_style.toml（共通探索先）の [naming_policy] を読む（無ければ None）。

    探索先は _load_house_style_rules と同じ house_style_paths。先に見つかった
    方針を返す（work 側 → 親側）。
    """
    from vocab import house_style_paths
    for p in house_style_paths(work):
        try:
            data = load_toml(p)
        except Exception:
            continue
        pol = data.get("naming_policy") if isinstance(data, dict) else None
        if isinstance(pol, dict):
            return pol
    return None


def _fmt_vocab_selection(work: Path, body_texts: list[str]) -> list[str]:
    """統合辞書から対象ブロックの原文に出現する語を選別し、訳語セクションを作る（P0-3）。

    作品 terms は既存の用語集の選別が扱うため、ここでは house_style と拡張
    vocab.toml のエントリ（別字形・別表記の経路を含む）だけを追加する。
    差し込む語が無ければ空リストを返す（何もしない）。
    """
    from vocab import load_vocab
    selected = [e for e in load_vocab(work).select_by_source(body_texts)
                if e.origin != "terms"]
    if not selected:
        return []
    lines = ["## 訳語(辞書。原文に出現した語はこれに従う)"]
    for e in selected:
        head = f"{e.src}（{' / '.join(e.variants)}）" if e.variants else e.src
        lines.append(f"- {head} → {e.dst or '(未規定)'} [{e.kind}]")
    lines.append("")
    return lines


# ── 台帳（ブロック横断状態の外部化。レビュー提案1） ──────────────────────
# 確定済みブロックで消費したルビ・用語初出・申告済みオノマトペを「消費済み」と
# して翻訳コンテキストへ注入し、再付与・重複を防ぐ。上限を超えた分は
# 「…他N件」で省略する（status の一覧と同じ形式）。

LEDGER_LIST_CAP = 8


def _fmt_capped(items: list[str], cap: int = LEDGER_LIST_CAP) -> str:
    """一覧を上限まで並べ、超過分は「…他N件」で省略する。"""
    shown = ", ".join(items[:cap])
    if len(items) > cap:
        shown += f" …他{len(items) - cap}件"
    return shown


def _fmt_ledger_consumed(work: Path) -> list[str]:
    """台帳の「消費済み」節（翻訳モードのみ。データが無ければ節ごと出さない）。

    - 消費済みルビ（再付与不可）: [[ruby_applied]] の term
    - 使用済みオノマトペ: [[ono_used]] の form（context があれば括弧付き）
    - 用語の初出済み: [[term_first_use]] の term
    """
    try:
        from ledger import load_ledger
        data = load_ledger(work)
    except Exception:
        return []
    out: list[str] = []

    ruby_terms: list[str] = []
    for r in data.get("ruby_applied", []):
        t = str(r.get("term") or "").strip()
        if t and t not in ruby_terms:
            ruby_terms.append(t)
    if ruby_terms:
        # 見出しは「この作品で」を明示する（G10 / T13）。台帳は作品ごとに独立で、
        # 前作の消費済みルビは carry で引き継がない（初出ルビは作品単位）。
        out += ["## この作品で消費済みルビ（再付与不可）", _fmt_capped(ruby_terms), ""]

    ono_items: list[str] = []
    for o in data.get("ono_used", []):
        form = str(o.get("form") or "").strip()
        if not form:
            continue
        ctx = str(o.get("context") or "").strip()
        item = f"{form}（{ctx}）" if ctx else form
        if item not in ono_items:
            ono_items.append(item)
    if ono_items:
        out += ["## 使用済みオノマトペ", _fmt_capped(ono_items), ""]

    term_items: list[str] = []
    for t in data.get("term_first_use", []):
        v = str(t.get("term") or "").strip()
        if v and v not in term_items:
            term_items.append(v)
    if term_items:
        out += ["## 用語の初出済み", _fmt_capped(term_items), ""]

    return out


_kind_map = {
    "naming": "命名",
    "ruby": "ルビ",
    "keishou": "敬称",
    "jishou": "自称",
    "ninshou": "二人称",
    "kigou": "記号",
    "kugirisen": "区切り線",
    "goi": "語彙",
    "sonota": "その他",
}


#: [naming_policy].codename の表示（G14 / T3。値は katakana / sinitic / keep）
_CODENAME_LABELS = {
    "katakana": "カタカナ英語",
    "sinitic": "漢語のまま",
    "keep": "原語のまま",
}


def _fmt_naming_policy(policy: dict | None) -> list[str]:
    """[naming_policy] をコンテキストへ描画する（G15 / T6）。

    人間が house_style.toml に書いた命名方針は rules と違い従来どこにも
    描画されず、翻訳コンテキストへ届かなかった（dead config）。style は
    人名、codename はコードネーム（代号）の扱いで、別キーとして出す。
    """
    if not isinstance(policy, dict):
        return []
    style = str(policy.get("style") or "").strip()
    scope = str(policy.get("scope") or "").strip()
    codename = str(policy.get("codename") or "").strip()
    decided = str(policy.get("decided_at") or "").strip()
    note = str(policy.get("note") or "").strip()
    if not style and not codename:
        return []
    out: list[str] = []
    if style:
        details = ", ".join(x for x in (
            f"scope={scope}" if scope else "",
            f"decided_at={decided}" if decided else "",
        ) if x)
        out.append(f"- 命名方針: {style}" + (f"（{details}）" if details else ""))
    if codename:
        label = _CODENAME_LABELS.get(codename, codename)
        out.append(f"- コードネーム: {label}（codename={codename}）")
    if note:
        out.append(f"- 命名方針の根拠: {note}")
    return out


def _fmt_house_style(rules: list[dict], work_id: str,
                     naming_policy: dict | None = None) -> str:
    """approved/locked かつ該当 scope のルールと [naming_policy] を整形する。

    scope=series は共通探索先（work/_house → work.parent/_house）に置かれた
    連作共有ルールとして適用する（#43。従来は黙って無視されていた。target は
    記録用で、配下の作品へ共通適用される）。[naming_policy] は先頭に描画する。
    """
    lines = _fmt_naming_policy(naming_policy)
    for r in rules:
        status = r.get("status", "")
        if status not in ("approved", "locked"):
            continue
        scope = r.get("scope", "")
        if scope in ("global", "series"):
            pass
        elif scope == "work" and r.get("target") == work_id:
            pass
        else:
            continue

        kind = r.get("kind", "")
        pattern = r.get("pattern", "")
        replacement = r.get("replacement", "")
        preference = r.get("preference", "")
        rationale = r.get("rationale", "")
        val = replacement or preference or ""
        prefix = _kind_map.get(kind, kind)
        rationale_part = f"（根拠: {rationale}）" if rationale else ""
        lines.append(f"- {prefix}: {pattern} → {val}{rationale_part}")
    return "\n".join(lines)


def get_work_id(work: Path) -> str:
    return work.name


#: ルビ方針（設定 [translation].ruby）の説明（G9: dead config のコンテキスト注入への接続）。
RUBY_MODES = {
    "first": "人名・固有名詞の初出のみ",
    "always": "初出に限らず付与",
    "none": "ルビを振らない",
}


def ruby_mode(cfg: dict) -> str:
    """設定 [translation].ruby を解釈する（欠落・未知値は first=既定）。"""
    tr = (cfg or {}).get("translation")
    v = str((tr if isinstance(tr, dict) else {}).get("ruby") or "first").strip().lower()
    return v if v in RUBY_MODES else "first"


def _warn_unnormalized_images(work: Path) -> None:
    """analysis モードの保険 (P0-2): 未正規化の画像を stderr に1行で警告する（停止しない）。

    後から normalize_images.py を実行すると参照の書き換えで chapters/*.paras.toml が
    変わり analysis_src_hash が変わる——確定済みの分析が無効になる前に気づかせる。
    """
    bad = find_unnormalized_images(work / "output" / "images")
    if not bad:
        return
    shown = ", ".join(bad[:5]) + (f" ほか{len(bad) - 5}件" if len(bad) > 5 else "")
    print(
        f"[context] 警告: 未正規化の画像が {len(bad)} 件あります: {shown}。"
        "先に normalize_images.py --apply で正規化してください"
        "（後から正規化すると参照の書き換えで analysis_src_hash が変わり、確定済みの分析が無効になります）",
        file=sys.stderr,
    )


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="コンテキスト生成")
    ap.add_argument("work")
    ap.add_argument("--chapter", required=True, help="例: ch001")
    ap.add_argument("--block", default=None, help="例: b1(未指定=未完了の先頭)")
    ap.add_argument("--mode", choices=["analysis", "translation"], default="translation")
    ap.add_argument("--full", action="store_true", help="compact モードでも全件出力")
    ap.add_argument("--plot-budget", type=int, default=None,
                    help="あらすじの文字数予算。超過分は古い側を1行に省略（0=無制限。"
                         "既定: config [context].plot_budget_chars、無ければ "
                         f"{DEFAULT_PLOT_BUDGET}）")
    args = ap.parse_args()

    work = Path(args.work)
    _, dst = direction(work)
    meta_all = load_chapters_meta(work)
    chapters = meta_all["chapters"]
    ci = next((i for i, c in enumerate(chapters) if c["id"] == args.chapter), None)
    if ci is None:
        raise SystemExit(f"章が見つかりません: {args.chapter}")
    blocks = chapters[ci]["blocks"]
    if args.block:
        bid = args.block
    else:
        suf = target_suffix(dst)
        bi = next((i for i, b in enumerate(blocks)
                   if not (work / "output" / args.chapter / f"{b['id']}{suf}.md").exists()), 0)
        bid = blocks[bi]["id"]
    cid = args.chapter

    # context_style: analysis は full 固定、translation は --full で full、デフォルト compact
    if args.mode == "analysis":
        context_style = "full"
    else:
        context_style = "full" if args.full else "compact"

    with timed_stage(work, "build_context", chapter=cid, block=bid):
        _stage_impl(work, cid, bid, args.mode, context_style,
                    plot_budget=args.plot_budget)


def _stage_impl(
    work: Path, cid: str, bid: str, mode: str,
    context_style: str = "compact",
    plot_budget: int | None = None,
) -> None:
    if mode == "analysis":
        _warn_unnormalized_images(work)
    from vocab import house_style_misplaced
    _mp = house_style_misplaced(work)
    if _mp is not None:
        print(f"[context] 警告: {_mp} は読まれません（探索先は _house/house_style.toml）。"
              "`python3 scripts/house_style.py init <work>` で正しい場所に作れます")
    _, dst = direction(work)
    suf = target_suffix(dst)
    meta_all = load_chapters_meta(work)
    chapters = meta_all["chapters"]
    cfg = load_config()
    k_prev = int(cfg.get("context", {}).get("prev_tail_paras", 3))
    k_next = int(cfg.get("context", {}).get("next_head_paras", 2))
    if plot_budget is None:
        # あらすじ予算（G20）: --plot-budget > config [context].plot_budget_chars > 既定。
        # saga.py prepare は config 経由で受ける（フラグを渡さない呼び出しでも有効）。
        plot_budget = int(cfg.get("context", {}).get("plot_budget_chars", DEFAULT_PLOT_BUDGET))

    ci = next((i for i, c in enumerate(chapters) if c["id"] == cid), None)
    if ci is None:
        raise SystemExit(f"章が見つかりません: {cid}")
    blocks = chapters[ci]["blocks"]
    bi = next(i for i, b in enumerate(blocks) if b["id"] == bid)
    block = blocks[bi]

    chdir = work / "chapters"
    paras = load_paras(work, cid)["paras"]
    s, e = block["paras"]
    body = paras[s - 1:e]
    body_texts = [p["text"] for p in body]

    chars, terms, plot = load_analysis(work)

    titles = load_titles(work)
    src_title = (meta_all.get("work") or {}).get("title") or ""
    tr_title = target_title(titles, dst)
    work_line = f"作品: {tr_title or src_title or '(無題)'} / 作者: {(meta_all.get('work') or {}).get('author') or '(不明)'}"
    if tr_title and src_title and tr_title != src_title:
        work_line += f" / 原題: {src_title}"
    lines = [
        f"# 翻訳コンテキスト: {cid} / {bid}(モード: {mode})",
        "",
        # 段落をまたぐ台詞の閉じ漏れ対策（P0-1）: 冒頭で閉じ方を明示する。
        QUOTE_RULE,
        QUOTE_RULE_EXAMPLE,
        "",
        work_line,
    ]
    try:
        manifest = load_structured(work / "manifest")
    except FileNotFoundError:
        manifest = {}
    lang = manifest.get("lang") or meta_all.get("work", {}).get("lang") or ""
    han_script = manifest.get("han_script")
    lang_line = f"原語: {lang or '(未判定)'}"
    if han_script:
        lang_line += f" ({han_script})"
    if lang:
        pair = f"{lang}2{dst}"
        lang_line += f" / 訳文言語: {dst} / 言語tips: references/lang_{pair}.md （ファイルがあれば読め。無ければ汎用プロンプトのみ）"
    lines += [lang_line, ""]

    if mode == "translation" and dst == "ja":
        # ルビ方針（設定 [translation].ruby を翻訳コンテキストへ注入。G9: dead config の接続）
        rm = ruby_mode(cfg)
        lines += [f"ルビ方針: {rm}（{RUBY_MODES[rm]}）", ""]

    compact = (context_style == "compact")
    work_id = get_work_id(work)

    if compact:
        # ── compact モード ──
        prev_raw = prev_tail_text_raw(work, chapters, ci, bi)

        # 人物絞り込み (B)
        compact_chars = _compact_characters(chars, body_texts, prev_raw)
        total_chars = len(chars.get("characters", []))
        if compact_chars:
            lines += [
                "## 登場人物表(訳語と声はこれに従う)",
                fmt_characters({"characters": compact_chars}),
                "",
            ]
        else:
            lines += [
                "## 登場人物表(訳語と声はこれに従う)",
                "(このブロックに固定訳語が必要な人物は未登場)",
                "",
            ]

        # arcs 時点解決 (D): 欄ごとに累積し、対象ブロックの途中の変化も反映する（#7b）
        compact_arcs_data, arc_warns = _compact_arcs(chars, cid, bid, include_target=True)
        arc_count = sum(len(v) for v in compact_arcs_data.values())
        arcs_display = fmt_arcs_resolved(compact_arcs_data)
        if arc_warns:
            lines += arc_warns + [""]
        lines += [
            "## 声の変化(arcs。対象ブロック以降に効く一人称・口調・語彙)",
            arcs_display,
            "",
        ]

        # 用語絞り込み (C)
        compact_terms_list = _compact_terms(terms, body_texts, prev_raw)
        total_terms = len(terms.get("terms", []))
        lines += ["## 用語集", fmt_terms({"terms": compact_terms_list}), ""]

        # 訳語の差し込み (P0-3): 統合辞書（house_style / 拡張 vocab.toml）から
        # 対象ブロックの原文に出る語を選別して追加する（terms は既存の選別のまま）。
        if mode == "translation":
            lines += _fmt_vocab_selection(work, body_texts)
            # 台帳の「消費済み」節（レビュー提案1。データが無ければ出ない）
            lines += _fmt_ledger_consumed(work)

        # plot 時点解決 (E) + 文字数予算（G20: 超過分は古い側を1行に省略）
        plot_texts, plot_resolved = _compact_plot(plot, cid, bid, budget=plot_budget)
        if not plot_texts:
            plot_display = "(未記録)"
        else:
            plot_display = "\n".join(f"- {l}" for l in plot_texts)
        lines += [
            "## これまでのあらすじ(ネタバレ/翻訳用の内部情報)",
            plot_display,
            "",
        ]

        # house_style 差し込み (F): rules と [naming_policy] の両方を届ける（G15）
        house_rules = _load_house_style_rules(work)
        house_policy = _load_house_style_policy(work)
        house_text = _fmt_house_style(house_rules, work_id, house_policy)
        if house_text:
            lines += ["## ハウススタイル(既決事項)", house_text, ""]

        # 前ブロック + 次ブロック（compact でも常に出す）
        lines += [
            f"## 前ブロックの訳文末尾({k_prev}段落)",
            prev_tail_text(work, chapters, ci, bi, k_prev),
            "",
            f"## 次ブロック冒頭(原文・参照用)",
            next_head_text(chapters, ci, bi, k_next, chdir),
            "",
        ]

        # 出力サイズ報告 (H)。compact で落ちた人物名も出す（P1-4。読む側が欠落に気づける）
        n_chars_shown = len(compact_chars)
        n_terms_shown = len(compact_terms_list)
        plot_count = len(plot_texts)
        house_count = len([r for r in house_rules if r.get("status") in ("approved", "locked")])
        policy_part = "+命名方針" if house_policy else ""
        omitted = _omitted_character_names(chars, compact_chars)
        omitted_part = f"（省略: {', '.join(omitted)}）" if omitted else ""
        print(
            f"[context] compact: 人物 {n_chars_shown}/{total_chars}{omitted_part} / 用語 {n_terms_shown}/{total_terms} "
            f"/ arcs {arc_count}件 / plot {plot_count}件 / house_style {house_count}件{policy_part}",
            file=sys.stderr,
        )

    else:
        # ── full モード（従来挙動） ──
        lines += [
            "## 登場人物表(訳語と声はこれに従う)",
            fmt_characters(chars),
            "",
            "## 声の変化(arcs。対象ブロック以降に効く一人称・口調・語彙)",
            fmt_arcs(chars),
            "",
            "## 用語集",
            fmt_terms(terms),
            "",
            "## これまでのあらすじ(ネタバレ/翻訳用の内部情報)",
            fmt_plot(plot, budget=plot_budget),
            "",
            f"## 前ブロックの訳文末尾({k_prev}段落)",
            prev_tail_text(work, chapters, ci, bi, k_prev),
            "",
            f"## 次ブロック冒頭(原文・参照用)",
            next_head_text(chapters, ci, bi, k_next, chdir),
            "",
        ]

        # 訳語の差し込み (P0-3。翻訳コンテキストのみ)
        if mode == "translation":
            lines += _fmt_vocab_selection(work, body_texts)
            # 台帳の「消費済み」節（レビュー提案1。データが無ければ出ない）
            lines += _fmt_ledger_consumed(work)

    # 分析モードの生 TOML 廃止 (G): 整形済み人物表は full/compact 両方で既に出ている
    # 旧来の生 TOML ブロックは出力しない

    lines.append(f"## 対象本文({cid} {bid}: 段落{s}〜{e}, {len(body)}段落)")
    for p in body:
        lines.append(f"[P{p['n']:03d}] {p['text']}")
        lines.append("")

    out = work / "translation_input" / f"{cid}_{bid}_{mode}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    save_json(work / "logs" / "last_context.json", {"chapter": cid, "block": bid, "mode": mode, "path": str(out)})
    out.write_text("\n".join(lines), encoding="utf-8")

    if mode == "analysis":
        from datetime import datetime, timezone
        block["analysis_started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_chapters_meta(work, meta_all)

    n_chars = sum(len(p["text"]) for p in body)
    print(f"[context] {out}")
    print(f"[context] {cid}/{bid} paras={len(body)} chars={n_chars}")


if __name__ == "__main__":
    main()