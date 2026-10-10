"""訳文ドラフトの機械QA → 合格なら正式出力へ確定(--emit)。

--emit の確定は saga.py の共通確定処理（_finalize_block）を通る（#17）。
align / QA / 残渣の検査に通らない確定経路は無い。
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    acquire_lock,
    atomic_write_text,
    block_paras,
    character_aliases,
    direction,
    find_non_jis_han,
    load_chapters_meta,
    load_characters,
    load_config,
    load_json,
    load_paras,
    load_terms,
    load_toml,
    release_lock,
    save_chapters_meta,
    save_json,
    target_quotes,
    target_suffix,
    utf8,
)

PMARK = re.compile(r"^\[P(\d+)\]\s*(.*)$")  # 段落番号は1桁から可（1000段未満の作品で [P1] 形式になるため）


def parse_draft(text: str) -> tuple[list[tuple[int, str]], list[str]]:
    """([番号, 訳文]) の列と問題リストを返す。"""
    items: list[tuple[int, str]] = []
    problems: list[str] = []
    cur_num = None
    cur_text: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        m = PMARK.match(raw.strip())
        if m:
            if cur_num is not None:
                items.append((cur_num, "\n".join(cur_text).strip()))
            cur_num, cur_text = int(m.group(1)), [m.group(2)]
        elif cur_num is not None:
            cur_text.append(raw)
    if cur_num is not None:
        items.append((cur_num, "\n".join(cur_text).strip()))
    return items, problems


# ── ルビ検査（G8/G11。zh→ja の訳文はルビが付いているべき側） ──────────────
# 旧形式 親{読み} の検出器は ledger.VFM_RE の近傍（ledger.find_old_style_ruby）が正本。
# ここでは VFM の記法異常と、宣言済みルビの未適用を検出する。

_RUBY_CJK = re.compile(r"[\u4e00-\u9fff]")                     # 表記に漢字を含むか
_QA_VFM_TIGHT = re.compile(r"\{([^{}|]+)\|([^{}|]+)\}")        # 正常な VFM {親|読み}
_QA_VFM_LOOSE = re.compile(r"\{([^{}\n|]*)\|[^{}\n]*\}")       # 波括弧内に | がある束（親側を捕捉）
_QA_VFM_UNCLOSED = re.compile(r"\{[^{}\n]*\|[^{}\n]*")         # 閉じないまま行末
_QA_RB = re.compile(r"\[\[rb:([^>\]\n]*)(?:>[^\]\n]*)?\]\]")   # [[rb:親>読み]] / [[rb:親]]


def _ruby_declarations(work: Path) -> list[dict]:
    """ルビ宣言（terms / characters の ruby 欄）を返す。

    ``[{ja, src, ruby, kind}]``。表記に漢字を含まない宣言（カタカナ名など）は
    ルビの対象外として落とす（振っても意味が無く、未適用の誤報になるため）。
    """
    out: list[dict] = []
    try:
        terms = load_terms(work).get("terms") or []
    except Exception:
        terms = []
    for t in terms:
        if not isinstance(t, dict):
            continue
        src = str(t.get("term_src") or "").strip()
        ja = str(t.get("term_ja") or "").strip() or src
        ruby = str(t.get("ruby") or "").strip()
        if ja and ruby and _RUBY_CJK.search(ja):
            out.append({"ja": ja, "src": src or ja, "ruby": ruby, "kind": "term"})
    try:
        chars = load_characters(work).get("characters") or []
    except Exception:
        chars = []
    for c in chars:
        if not isinstance(c, dict):
            continue
        src = str(c.get("name_src") or "").strip()
        ja = str(c.get("name_ja") or "").strip() or src
        ruby = str(c.get("ruby") or "").strip()
        if ja and ruby and _RUBY_CJK.search(ja):
            out.append({"ja": ja, "src": src or ja, "ruby": ruby, "kind": "character"})
    return out


def _ruby_first_blocks(work: Path, words: list[dict]) -> dict[str, str]:
    """各語の原文初出ブロック（"chNNN/bN"）を章順・ブロック順の走査で返す。

    原文のどこにも現れない語は含めない（初出を判定できないため検査側で外す）。
    """
    remaining = {w["src"] for w in words if w["src"]}
    first: dict[str, str] = {}
    if not remaining:
        return first
    try:
        meta = load_chapters_meta(work)
    except Exception:
        return first
    for c in meta.get("chapters") or []:
        cid = str(c.get("id") or "")
        try:
            paras_data = load_paras(work, cid)
        except Exception:
            continue
        for b in c.get("blocks") or []:
            if not remaining:
                return first
            text = "\n".join(str(p.get("text") or "") for p in block_paras(paras_data, b))
            for src_form in list(remaining):
                if src_form in text:
                    first[src_form] = f"{cid}/{b.get('id')}"
                    remaining.discard(src_form)
    return first


def _ruby_applied_bases(text: str, rest: str) -> list[str]:
    """本文中のルビ親文字を列挙する（適用済み判定用）。

    VFM / 壊れた VFM（未閉じ含む）/ [[rb:]] の親側。壊れた記法でも親側が一致
    すれば「記法異常」側の1件に集約する（未適用との二重報告を避ける）。
    """
    bases = [m.group(1).strip() for m in _QA_VFM_LOOSE.finditer(text)]
    bases += [m.group(0)[1:].split("|", 1)[0].strip()
              for m in _QA_VFM_UNCLOSED.finditer(rest)]
    bases += [m.group(1).strip() for m in _QA_RB.finditer(text)]
    return [b for b in bases if b]


def find_ruby_issues(work: Path, text: str, cid: str, bid: str,
                     cfg: dict | None = None) -> dict:
    """zh→ja 訳文のルビ検査（G8）。未適用・旧形式・記法異常を検出する。

    戻り値 ``{not_applied, old_style, malformed, note}``:

    - ``not_applied``: ruby を宣言した語（terms / characters）が原文初出ブロックで
      VFM / [[rb:]] 無しに現れる（台帳の消費済み・旧形式での付与は「付いている」側に
      倒す）。初出ブロック以外では初出のみの方針によりルビ無しが正なので検査しない。
    - ``old_style``: 旧形式 親{読み}（台帳・review の初出検査に載らない。警告のみ）。
    - ``malformed``: ``{親|}`` ``{|読み}`` ``{親|読み|…}`` ``{親|読み`` などの壊れた記法。
    - ``note``: 検査を省略した理由（設定 ruby="none"）。
    """
    from ledger import find_old_style_ruby  # noqa: E402
    old = find_old_style_ruby(text)

    malformed: list[str] = []
    for m in _QA_VFM_LOOSE.finditer(text):
        if not _QA_VFM_TIGHT.fullmatch(m.group(0)):
            malformed.append(m.group(0))
    rest = _QA_VFM_LOOSE.sub("", text)  # 閉じた束を除いた残りから未閉じを探す
    for m in _QA_VFM_UNCLOSED.finditer(rest):
        malformed.append(m.group(0))

    mode = "first"
    tr = (cfg or {}).get("translation")
    if isinstance(tr, dict):
        v = str(tr.get("ruby") or "first").strip().lower()
        if v in ("first", "always", "none"):
            mode = v

    note = ""
    not_applied: list[dict] = []
    if mode == "none":
        note = "設定 ruby=none のため未適用検査は省略"
    else:
        words = _ruby_declarations(work)
        if words:
            first_blocks = _ruby_first_blocks(work, words)
            try:
                from ledger import load_ledger  # noqa: E402
                consumed = [str(r.get("term") or "")
                            for r in (load_ledger(work).get("ruby_applied") or [])]
            except Exception:
                consumed = []
            bases = _ruby_applied_bases(text, rest)
            cur = f"{cid}/{bid}"
            for w in words:
                ja = w["ja"]
                if ja not in text:
                    continue
                first = first_blocks.get(w["src"])
                if first is None or first != cur:
                    continue  # 原文初出ブロック以外はルビ無しが正（初出のみの方針）
                if any(ja == b or ja in b for b in bases):
                    continue  # VFM / [[rb:]] で付いている
                if any(ja == t or ja in t or t in ja for t in consumed):
                    continue  # 台帳で消費済み（前のブロックで付与済み）
                if any(ja == e["base"] or ja in e["base"] or e["base"] in ja for e in old):
                    continue  # 旧形式で付いている（記法の警告側で知らせる）
                not_applied.append({"kind": w["kind"], "term": ja,
                                    "ruby": w["ruby"], "first_block": first})
    return {"not_applied": not_applied, "old_style": old,
            "malformed": malformed, "note": note}


def run_qa(work: Path, cid: str, bid: str, draft: Path, strict: bool = True) -> tuple[dict, list]:
    blocks = next(c["blocks"] for c in load_chapters_meta(work)["chapters"] if c["id"] == cid)
    s, e = next(b["paras"] for b in blocks if b["id"] == bid)
    expected = list(range(s, e + 1))

    text = draft.read_text(encoding="utf-8")
    items, problems = parse_draft(text)
    nums = [n for n, _ in items]

    # 画像リンク ![](images/...) は実ファイル名(=原語表記)を保持せねばならず、
    # 訳文の用字チェック対象ではない。本文点検用にタグを除去したビューを作る。
    _IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
    qa_items = [(n, _IMG_RE.sub("", t)) for n, t in items]

    report = {"chapter": cid, "block": bid, "draft": str(draft), "checks": {}}
    ok = True
    src, dst = direction(work)
    qopen, qclose = target_quotes(dst)
    report["direction"] = f"{src}2{dst}"

    seq_ok = nums == expected
    report["checks"]["paragraph_sequence"] = {"pass": seq_ok, "expected_first": expected[0], "expected_last": expected[-1], "got": nums[:5] if len(nums) < 8 else f"{nums[0]}..{nums[-1]} n={len(nums)}"}
    if not seq_ok:
        ok = False

    empties = [n for n, t in items if not t.strip()]
    report["checks"]["empty_paragraphs"] = {"pass": not empties, "empty": empties}
    if empties:
        ok = False

    chars = load_characters(work).get("characters") or []
    leftover = []
    for c in chars:
        src_name = c.get("name_src", "")
        ja = c.get("name_ja", "")
        if not src_name or ja == src_name:
            continue
        # フルネーム+区切り(-/·/・‐)で分割した部分名の両方を照合する。
        # 訳文に残るのは部分名だけのことが多い(例: 张三-李四 → 张三)
        parts = [p for p in re.split(r"[-·・‐—_ ]+", src_name) if len(p) >= 2]
        candidates = [src_name] + parts if len(src_name) >= 2 else parts
        # aliases（別名・愛称・原文の異表記）の各形も照合対象にする（P0-4）。
        # 別名が訳文に原語のまま残るのを検出する。
        # G42: かなを含む別名は「訳文に残す日本語名」の誤登録の疑いが濃い
        # （実害2作）。原文照合から外す（定義は references/data-schema.md）。
        alias_forms = [a for a in character_aliases(c) if a not in candidates]
        if dst == "ja":
            alias_forms = [a for a in alias_forms
                           if not re.search(r"[\u3040-\u30ff]", a)]
        candidates += alias_forms
        for name in candidates:
            if name in ja:
                continue  # 訳語にそのまま含まれる漢字(例: 白狼→暗影団の頭目・白狼)は残留扱いしない
            cnt = sum(1 for _, t in qa_items if name in t)
            if cnt:
                leftover.append({"name_src": name, "name_ja": ja, "count": cnt})
                break
    report["checks"]["leftover_source_names"] = {"pass": not leftover, "found": leftover}
    if leftover:
        ok = False

    if dst == "zh":
        # 日本語の残渣チェック（ターゲット側の機械検証）。
        # 仮名は文字種で一意に検出できる。簡体字訳に残ってはならない。
        from detect.ja2zh import (  # noqa: E402
            find_jp_quotes, find_kana, find_non_gbk_han, find_rb_kana,
            find_trad_markers, find_vfm,
        )
        body_text = "\n".join(t for _, t in qa_items)
        kana = find_kana(body_text)
        report["checks"]["kana_leftover"] = {"pass": not kana, "found": kana}
        if kana:
            ok = False
        quotes = find_jp_quotes(body_text)
        report["checks"]["jp_quotes_leftover"] = {"pass": not quotes, "found": quotes}
        if quotes:
            ok = False
        vfm = find_vfm(body_text)
        rb = find_rb_kana(body_text)
        report["checks"]["ruby_leftover"] = {"pass": not (vfm or rb), "found": (vfm + rb)[:10]}
        if vfm or rb:
            ok = False
        nongb = find_non_gbk_han(body_text)
        report["checks"]["non_gbk_han"] = {"pass": not nongb, "found": nongb}
        if nongb:
            ok = False
        trad = find_trad_markers(body_text)
        report["checks"]["trad_leftover"] = {"pass": not trad, "found": trad}
        if trad:
            ok = False
    else:
        # 日本語に無い漢字(CP932外 + 言語packのNON_JIS_HAN)の残存チェック。
        # 登場人物表に無い原語(誤字・未登録の固有名詞・簡体字混入)を拾う。
        # 原文の誤字を原文ママで訳出すると、その簡体字が完成後まで残る。
        # このチェックでブロック確定時に落とす。
        non_jis = []
        for ch, cnt in find_non_jis_han("\n".join(t for _, t in qa_items)):
            non_jis.append({"char": ch, "count": cnt})
        report["checks"]["non_jis_han"] = {"pass": not non_jis, "found": non_jis}
        if non_jis:
            ok = False

        if dst == "ja":
            # ルビ検査（G8: 従来は if dst=="zh" 内にあり zh→ja では走らなかった）。
            # 宣言済みルビの未適用・旧形式・記法異常を検出する。
            # 旧形式は警告のみ（台帳・review に載らない旨を commit 側が知らせる）。
            ruby = find_ruby_issues(work, "\n".join(t for _, t in qa_items),
                                    cid, bid, cfg=load_config())
            na = {"pass": not ruby["not_applied"], "found": ruby["not_applied"]}
            if ruby["note"]:
                na["note"] = ruby["note"]
            report["checks"]["ruby_not_applied"] = na
            if ruby["not_applied"]:
                ok = False
            report["checks"]["ruby_notation"] = {
                "pass": not ruby["malformed"],
                "old_style": ruby["old_style"],
                "malformed": ruby["malformed"],
            }
            if ruby["malformed"]:
                ok = False

    # [Pn] の境界を1つでも消す融合は原文と1対1の原則を壊すため、全て fatal にする
    # （2段落の融合も通過させない）。同一 [Pn] 内の複数行（歌・詩・折り返し）は
    # 境界を消さないため検出対象外。失敗理由の分類は timeline の
    # merged_paragraphs（stage2）を使う。
    merged = find_merged_paragraphs(items, qopen, qclose)
    report["checks"]["merged_paragraphs"] = {"pass": not merged, "found": merged}
    if merged:
        ok = False

    # 最後の段落の閉じ漏れ（文末で引用が閉じていない）は別の検査。
    # 段落をまたぐ閉じ漏れは上の merged_paragraphs が検出するので、ここは文末だけを見る。
    unclosed = []
    if items:
        n_open, n_close = items[-1][1].count(qopen), items[-1][1].count(qclose)
        if n_open > n_close:
            unclosed.append({"para": items[-1][0], "open": n_open, "close": n_close})
    report["checks"]["unclosed_quote"] = {"pass": not unclosed, "found": unclosed}
    if unclosed:
        ok = False

    report["pass"] = ok
    return report, items


QOPEN = "「"
QCLOSE = "」"


def layout_lines(text: str, qopen: str = QOPEN, qclose: str = QCLOSE) -> str:
    """全行空行レイアウト(行間は必ず空行1つ)。

    「…」が閉じていない間の行は単一改行で継続する(歌詞など複数行ブロックを保つ)。
    セリフ行・地の文・画像行・見出し行はそれぞれ独立ブロックになる。
    """
    blocks: list[str] = []
    cur: list[str] = []
    opens = closes = 0
    for raw in text.replace("\r\n", "\n").split("\n"):
        ln = raw.strip()
        if not ln:
            continue
        if cur and opens <= closes:
            blocks.append("\n".join(cur))
            cur = []
        cur.append(ln)
        opens += ln.count(qopen)
        closes += ln.count(qclose)
    if cur:
        blocks.append("\n".join(cur))
    return "\n\n".join(blocks)


def find_merged_paragraphs(items: list[tuple[int, str]], qopen: str = QOPEN, qclose: str = QCLOSE) -> list[dict]:
    """段落をまたいだ閉じ漏れで、段落の区切りが消えた範囲を返す。

    layout_lines は引用が閉じるまで空行を捨てて連結する。日本語は段落ごとに
    台詞を閉じる規約なので、段落の区切りが消えていれば閉じ漏れ事故(中国語
    原作の段落跨ぎ台詞をそのまま写したもの)とみなす。1 段落の内部改行
    (歌・詩)は融合にならない。

    段落の区切りに一意のマーカー行を挟んで layout_lines を走らせ、本文と
    同居したまま残ったマーカーから、融合した範囲を出す。
    戻り値: [{"first": 先頭段落番号, "last": 末尾段落番号, "count": 段落数}]
    """
    if len(items) < 2:
        return []
    sent = "\x00"
    parts: list[str] = []
    for i, (_, t) in enumerate(items):
        parts.append(t)
        if i < len(items) - 1:
            parts.append(f"{sent}i{i}{sent}")
    merged: list[dict] = []
    for block in layout_lines("\n\n".join(parts), qopen, qclose).split("\n\n"):
        lines = block.split("\n")
        inside: list[int] = []
        for ln in lines:
            if len(ln) > 2 and ln.startswith(sent) and ln.endswith(sent):
                try:
                    inside.append(int(ln[2:-1]))
                except ValueError:
                    continue
        if not inside:
            continue
        if all(l.startswith(sent) and l.endswith(sent) for l in lines):
            continue  # マーカーだけのブロック = 融合していない
        first_i, last_i = inside[0], inside[-1]
        merged.append({"first": items[first_i][0], "last": items[last_i + 1][0],
                       "count": last_i - first_i + 2})
    return merged


def emit(work: Path, cid: str, bid: str, items: list[tuple[int, str]], archive: bool = False) -> None:
    _, dst = direction(work)
    suf = target_suffix(dst)
    qopen, qclose = target_quotes(dst)
    ja_dir = work / "output" / cid
    clean = layout_lines("\n\n".join(t for _, t in items), qopen, qclose) + "\n"
    atomic_write_text(ja_dir / f"{bid}{suf}.md", clean)
    # 対訳(段落対照)を中間物として残す。正式出力(章対訳・全文対訳)は assemble が作る
    src_map = {p["n"]: p["text"] for p in load_paras(work, cid)["paras"]}
    pairs = [f"> [P{n:03d}] {src_map.get(n, '')}\n\n{t}\n" for n, t in items]
    atomic_write_text(ja_dir / f"{bid}_parallel.md", "\n".join(pairs))
    # 確定ブロックの状態を chapters_meta に記録する（record_meta 忘れでも status が進む）
    meta = load_chapters_meta(work)
    for c in meta["chapters"]:
        if c["id"] == cid:
            for b in c["blocks"]:
                if b["id"] == bid:
                    b["status"] = "emitted"
                    b["emitted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if all(b.get("status") == "emitted" for b in c["blocks"]):
                c["status"] = "done"
            break
    save_chapters_meta(work, meta)
    draft = work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"
    if draft.exists():
        if archive:
            done_dir = work / "output" / "_work" / "done"
            done_dir.mkdir(parents=True, exist_ok=True)
            dest = done_dir / f"{cid}_{bid}{suf}_numbered.md"
            shutil.move(str(draft), str(dest))
        else:
            draft.unlink()
    qa_rep = work / "output" / "_work" / f"{cid}_{bid}_qa.json"
    if qa_rep.exists():
        qa_rep.unlink()


def main() -> None:
    from _lib import timed_stage
    utf8()
    ap = argparse.ArgumentParser(description="訳文QAと確定")
    ap.add_argument("work")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--block", required=True)
    ap.add_argument("--draft", default=None, help="既定: output/_work/chXXX_bY_<dst>_numbered.md（訳文言語による）")
    ap.add_argument("--emit", action="store_true", help="合格時に正式出力へ書き込む")
    ap.add_argument("--no-strict", action="store_true", help="段落数不一致でも警告のみ")
    ap.add_argument("--archive", action="store_true", help="ドラフト削除→_work/done/ への移動")
    args = ap.parse_args()
    with timed_stage(args.work, "qa_translation",
                     chapter=args.chapter, block=args.block):
        _stage_impl(args)


def _stage_impl(args: argparse.Namespace) -> None:

    work = Path(args.work)
    cid, bid = args.chapter, args.block

    if args.emit:
        # 確定は saga.py の共通確定処理（検査込み）に一本化した（#17）。
        suf = target_suffix(direction(work)[1])
        canon = work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"
        if args.draft and Path(args.draft).resolve() != canon.resolve():
            print("[qa] エラー: --emit は既定の場所のドラフト（output/_work/）を検査・確定します。"
                  "別のドラフトは先に既定の場所へ置いてください", file=sys.stderr)
            sys.exit(1)
        sys.exit(_emit_via_common_finalize(work, cid, bid, archive=bool(args.archive)))

    suf = target_suffix(direction(work)[1])
    draft = Path(args.draft) if args.draft else work / "output" / "_work" / f"{cid}_{bid}{suf}_numbered.md"
    if not draft.exists():
        raise SystemExit(f"ドラフトがありません: {draft}")

    report, items = run_qa(work, cid, bid, draft, strict=not args.no_strict)
    save_json(work / "output" / "_work" / f"{cid}_{bid}_qa.json", report)

    for k, v in report["checks"].items():
        extra = ""
        if k == "ruby_notation" and v.get("old_style"):
            forms = "、".join(e["form"] for e in v["old_style"][:5])
            extra = f" 旧形式(警告): {forms}"
        if k == "ruby_not_applied" and v.get("note"):
            extra = f" {v['note']}"
        print(f"[qa] {k}: {'PASS' if v['pass'] else 'FAIL'} {'' if v['pass'] else v}{extra}")
    print(f"[qa] result: {'PASS' if report['pass'] else 'FAIL'}")
    sys.exit(0 if report["pass"] else 1)


def _emit_via_common_finalize(work: Path, cid: str, bid: str, archive: bool) -> int:
    """共通確定処理（saga._finalize_block）を共有ロックの内側で呼ぶ（#17）。

    commit / safe_emit と同じ検査（align / QA / 残渣）を通ってから確定する。
    戻り値は終了コード（0=確定または復旧の完遂、1=検査で停止、3=ロック中）。
    """
    ok, reason = acquire_lock(work, "qa_translation_emit")
    if not ok:
        print(f"[qa] エラー: {reason}（終了コード 3）", file=sys.stderr)
        return 3
    try:
        from saga import _finalize_block
        return _finalize_block(work, cid, bid, archive=archive)
    finally:
        release_lock(work)


if __name__ == "__main__":
    main()
