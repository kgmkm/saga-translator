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


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="コンテキスト生成")
    ap.add_argument("work")
    ap.add_argument("--chapter", required=True, help="例: ch001")
    ap.add_argument("--block", default=None, help="例: b1(未指定=未完了の先頭)")
    ap.add_argument("--mode", choices=["analysis", "translation"], default="translation")
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

    with timed_stage(work, "build_context", chapter=cid, block=bid):
        _stage_impl(work, cid, bid, args.mode)


def _stage_impl(work: Path, cid: str, bid: str, mode: str) -> None:
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
    lines += [
        lang_line,
        "",
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
    if mode == "analysis":
        cur = characters_toml_path(work)
        lines += ["## 現在の分析データ(この状態への追記・更新分を出す)",
                  "```toml",
                  cur.read_text(encoding="utf-8") if cur.exists() else "",
                  "```", ""]

    lines.append(f"## 対象本文({cid} {bid}: 段落{s}〜{e}, {len(body)}段落)")
    for p in body:
        lines.append(f"[P{p['n']:03d}] {p['text']}")
        lines.append("")

    out = work / "translation_input" / f"{cid}_{bid}_{mode}.md"
    save_json(work / "logs" / "last_context.json", {"chapter": cid, "block": bid, "mode": mode, "path": str(out)})
    out.write_text("\n".join(lines), encoding="utf-8")

    if mode == "analysis":
        from datetime import datetime, timezone
        block["analysis_done_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        save_chapters_meta(work, meta_all)

    n_chars = sum(len(p["text"]) for p in body)
    print(f"[context] {out}")
    print(f"[context] {cid}/{bid} paras={len(body)} chars={n_chars}")


if __name__ == "__main__":
    main()