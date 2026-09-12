"""訳文ドラフトの機械QA → 合格なら正式出力へ確定(--emit)。"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    atomic_write_text,
    find_non_jis_han,
    load_chapters_meta,
    load_characters,
    load_json,
    load_toml,
    save_chapters_meta,
    save_json,
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


def run_qa(work: Path, cid: str, bid: str, draft: Path, strict: bool = True) -> tuple[dict, list]:
    blocks = next(c["blocks"] for c in load_chapters_meta(work)["chapters"] if c["id"] == cid)
    s, e = next(b["paras"] for b in blocks if b["id"] == bid)
    expected = list(range(s, e + 1))

    text = draft.read_text(encoding="utf-8")
    items, problems = parse_draft(text)
    nums = [n for n, _ in items]

    report = {"chapter": cid, "block": bid, "draft": str(draft), "checks": {}}
    ok = True

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
        # フルネーム+区切り(-/·/・/‐)で分割した部分名の両方を照合する。
        # 訳文に残るのは部分名だけのことが多い(例: 菲娜莉-贝莉安娜 → 菲娜莉)
        parts = [p for p in re.split(r"[-·・‐—_ ]+", src_name) if len(p) >= 2]
        candidates = [src_name] + parts if len(src_name) >= 2 else parts
        for name in candidates:
            if name in ja:
                continue  # 訳語にそのまま含まれる漢字(例: 夜蜘→暗影監察長・夜蜘)は残留扱いしない
            cnt = sum(1 for _, t in items if name in t)
            if cnt:
                leftover.append({"name_src": name, "name_ja": ja, "count": cnt})
                break
    report["checks"]["leftover_source_names"] = {"pass": not leftover, "found": leftover}
    if leftover:
        ok = False

    # 日本語に無い漢字(CP932外 + 言語packのNON_JIS_HAN)の残存チェック。
    # 登場人物表に無い原語(誤字・未登録の固有名詞・簡体字混入)を拾う。
    # 実測(霊狐堕淵): 原文の誤字「汐剑海斗」を原文ママで訳出した結果、
    # 「剑」が完成後まで残った。このチェックでブロック確定時に落とす。
    non_jis = []
    for ch, cnt in find_non_jis_han("\n".join(t for _, t in items)):
        non_jis.append({"char": ch, "count": cnt})
    report["checks"]["non_jis_han"] = {"pass": not non_jis, "found": non_jis}
    if non_jis:
        ok = False

    report["pass"] = ok
    return report, items


QOPEN = "「"
QCLOSE = "」"


def layout_lines(text: str) -> str:
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
        opens += ln.count(QOPEN)
        closes += ln.count(QCLOSE)
    if cur:
        blocks.append("\n".join(cur))
    return "\n\n".join(blocks)


def emit(work: Path, cid: str, bid: str, items: list[tuple[int, str]]) -> None:
    ja_dir = work / "output" / cid
    clean = layout_lines("\n\n".join(t for _, t in items)) + "\n"
    atomic_write_text(ja_dir / f"{bid}_ja.md", clean)
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
    draft = work / "output" / "_work" / f"{cid}_{bid}_ja_numbered.md"
    if draft.exists():
        draft.unlink()
    qa_rep = work / "output" / "_work" / f"{cid}_{bid}_qa.json"
    if qa_rep.exists():
        qa_rep.unlink()


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="訳文QAと確定")
    ap.add_argument("work")
    ap.add_argument("--chapter", required=True)
    ap.add_argument("--block", required=True)
    ap.add_argument("--draft", default=None, help="既定: output/_work/chXXX_bY_ja_numbered.md")
    ap.add_argument("--emit", action="store_true", help="合格時に正式出力へ書き込む")
    ap.add_argument("--no-strict", action="store_true", help="段落数不一致でも警告のみ")
    args = ap.parse_args()

    work = Path(args.work)
    cid, bid = args.chapter, args.block
    draft = Path(args.draft) if args.draft else work / "output" / "_work" / f"{cid}_{bid}_ja_numbered.md"
    if not draft.exists():
        raise SystemExit(f"ドラフトがありません: {draft}")

    report, items = run_qa(work, cid, bid, draft, strict=not args.no_strict)
    save_json(work / "output" / "_work" / f"{cid}_{bid}_qa.json", report)

    for k, v in report["checks"].items():
        print(f"[qa] {k}: {'PASS' if v['pass'] else 'FAIL'} {'' if v['pass'] else v}")
    print(f"[qa] result: {'PASS' if report['pass'] else 'FAIL'}")

    if report["pass"] and args.emit:
        emit(work, cid, bid, items)
        print(f"[emit] {work / 'output' / cid / (bid + '_ja.md')}")
        sys.exit(0)
    sys.exit(0 if report["pass"] else 1)


if __name__ == "__main__":
    main()
