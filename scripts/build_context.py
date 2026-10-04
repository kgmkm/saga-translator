"""翻訳/分析用コンテキストの組み立て。エージェントは生成されたファイルだけを読む。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    characters_toml_path, direction, load_chapters_meta, load_characters, load_config, load_json,
    load_paras, load_plot, load_structured, load_terms, load_titles, load_toml,
    save_chapters_meta, save_json, target_suffix, target_title, utf8,
)


# prepare が出すコンテキストの冒頭に置く、段落をまたぐ台詞の引用閉じ規則（P0-1）。
# 文言は仕様で固定（改変禁止）。引用を開いたまま段落を終えると layout_lines が
# 段落の区切りを消して連結する（qa の merged_paragraphs が検出して commit を止める）。
QUOTE_RULE = "【段落の引用の閉じ方】段落をまたぐ台詞でも、その段落の終わりで 」 を閉じる。次の段落では 「 を開き直す。引用を開いたまま段落を終えないこと。"
QUOTE_RULE_EXAMPLE = "（例: 会話が2段落に続くとき、1段落目の終わりに 」 だけを置いて閉じる）"


def load_analysis(work: Path) -> tuple[dict, dict, dict]:
    return load_characters(work), load_terms(work), load_plot(work)


def fmt_characters(chars: dict) -> str:
    """翻訳コンテキスト用。声の欄を混ぜずに出す。旧 tone は speech_style が空のときだけ。"""
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
        add("役割", "role")
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


def fmt_plot(plot: dict, tail: int = 20) -> str:
    lines = [e.get("text", "") for e in plot.get("entries", [])]
    if not lines:
        return "(未記録)"
    return "\n".join(f"- {l}" for l in lines[-tail:])


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


def _compact_terms(
    terms: dict,
    body_texts: list[str],
    prev_translation: str,
) -> list[dict]:
    """compact モード: 用語を絞り込む。"""
    result = []
    seen = set()
    for t in terms.get("terms", []):
        term_src = t.get("term_src") or ""
        term_ja = t.get("term_ja") or ""
        is_core = bool(t.get("core"))

        in_src = bool(term_src) and any(term_src in bt for bt in body_texts)
        in_prev = bool(term_ja) and term_ja in prev_translation

        if is_core or in_src or in_prev:
            tid = id(t)
            if tid not in seen:
                seen.add(tid)
                result.append(t)
    return result


def _compact_arcs(
    chars: dict,
    cid: str, bid: str,
) -> tuple[dict, list[str]]:
    """compact モード: 対象ブロックより前に開始した最新 arc だけを人物ごとに出す。"""
    target = _block_address(cid, bid)
    warn_lines: list[str] = []
    result: dict[str, list[dict]] = {}
    for c in chars.get("characters", []):
        arcs = c.get("arcs") or []
        name = c.get("name_ja") or c.get("name_src") or ""
        before = []
        for a in arcs:
            at_block = a.get("at_block", "")
            if not at_block:
                continue
            parsed = _parse_at_block(at_block)
            if parsed is None:
                warn_lines.append(
                    f"# WARNING: 不正な at_block '{at_block}' "
                    f"(arc: {a.get('note', '')}) をスキップしました"
                )
                continue
            if parsed < target:
                before.append(a)
        if before:
            before.sort(key=lambda x: _parse_at_block(x.get("at_block", "") or (0, 0)), reverse=True)
            result[name] = [before[0]]
    return result, warn_lines


def _compact_plot(
    plot: dict,
    cid: str, bid: str,
    default_tail: int = 20,
) -> tuple[list[str], bool]:
    """compact モード: 対象ブロックより前の plot entries のみ。"""
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
        return [e.get("text", "") for e in entries[-default_tail:]], False

    texts = [e.get("text", "") for e in filtered[-default_tail:]]
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


def _fmt_house_style(rules: list[dict], work_id: str) -> str:
    """approved/locked かつ該当 scope のルールを整形。"""
    lines = []
    for r in rules:
        status = r.get("status", "")
        if status not in ("approved", "locked"):
            continue
        scope = r.get("scope", "")
        if scope == "global":
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


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="コンテキスト生成")
    ap.add_argument("work")
    ap.add_argument("--chapter", required=True, help="例: ch001")
    ap.add_argument("--block", default=None, help="例: b1(未指定=未完了の先頭)")
    ap.add_argument("--mode", choices=["analysis", "translation"], default="translation")
    ap.add_argument("--full", action="store_true", help="compact モードでも全件出力")
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
        _stage_impl(work, cid, bid, args.mode, context_style)


def _stage_impl(
    work: Path, cid: str, bid: str, mode: str,
    context_style: str = "compact",
) -> None:
    _, dst = direction(work)
    suf = target_suffix(dst)
    meta_all = load_chapters_meta(work)
    chapters = meta_all["chapters"]
    cfg = load_config()
    k_prev = int(cfg.get("context", {}).get("prev_tail_paras", 3))
    k_next = int(cfg.get("context", {}).get("next_head_paras", 2))

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

        # arcs 時点解決 (D)
        compact_arcs_data, arc_warns = _compact_arcs(chars, cid, bid)
        arc_count = sum(len(v) for v in compact_arcs_data.values())
        arcs_display = fmt_arcs({"characters": [
            {"name_ja": name, "name_src": name, "arcs": arcs}
            for name, arcs in compact_arcs_data.items()
        ]})
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

        # plot 時点解決 (E)
        plot_texts, plot_resolved = _compact_plot(plot, cid, bid)
        if not plot_texts:
            plot_display = "(未記録)"
        elif not plot_resolved:
            plot_display = "\n".join(f"- {l}" for l in plot_texts)
        else:
            plot_display = "\n".join(f"- {l}" for l in plot_texts)
        lines += [
            "## これまでのあらすじ(ネタバレ/翻訳用の内部情報)",
            plot_display,
            "",
        ]

        # house_style 差し込み (F)
        house_rules = _load_house_style_rules(work)
        house_text = _fmt_house_style(house_rules, work_id)
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

        # 出力サイズ報告 (H)
        n_chars_shown = len(compact_chars)
        n_terms_shown = len(compact_terms_list)
        plot_count = len(plot_texts)
        house_count = len([r for r in house_rules if r.get("status") in ("approved", "locked")])
        print(
            f"[context] compact: 人物 {n_chars_shown}/{total_chars} / 用語 {n_terms_shown}/{total_terms} "
            f"/ arcs {arc_count}件 / plot {plot_count}件 / house_style {house_count}件",
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
            fmt_plot(plot),
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