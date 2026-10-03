"""訳語変更の伝播。旧訳→新訳の置換候補を列挙し、確認のうえ適用する。"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import atomic_write_text, direction, save_json, target_suffix, utf8  # noqa: E402


def scan(work: Path, old: str) -> list[dict]:
    hits = []
    out_dir = work / "output"
    suf = target_suffix(direction(work)[1])
    for f in sorted(out_dir.rglob(f"*{suf}.md")):
        rel = f.relative_to(out_dir)
        text = f.read_text(encoding="utf-8")
        paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
        n = sum(p.count(old) for p in paras)
        if n:
            para_hits = [i + 1 for i, p in enumerate(paras) if old in p]
            hits.append({"file": str(rel), "total": n, "paragraphs": para_hits,
                         "multi_in_para": any(p.count(old) >= 2 for p in paras)})
    return hits


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="訳語変更の伝播")
    ap.add_argument("work")
    ap.add_argument("--term", required=True, help="旧訳語")
    ap.add_argument("--to", required=True, help="新訳語")
    ap.add_argument("--apply", action="store_true", help="実際に置換する(未指定は影響表示のみ)")
    args = ap.parse_args()

    work = Path(args.work)
    hits = scan(work, args.term)
    report = {"term": args.term, "to": args.to, "applied": False, "files": hits}
    total = sum(h["total"] for h in hits)
    if not hits:
        print(f"[rename] 「{args.term}」の出力への出現なし(影響0)")
    else:
        print(f"[rename] 「{args.term}」→「{args.to}」: {total}箇所 / {len(hits)}ファイル")
        for h in hits:
            flag = " ※同一段落に複数(文脈要確認)" if h["multi_in_para"] else ""
            print(f"  - {h['file']}: {h['total']}箇所{flag}")
    if args.apply and hits:
        for h in hits:
            f = work / "output" / h["file"]
            text = f.read_text(encoding="utf-8")
            atomic_write_text(f, text.replace(args.term, args.to))
        report["applied"] = True
        print(f"[rename] 適用完了。文脈依存の箇所(※付き)は該当ブロックの再翻訳を検討すること")
    save_json(work / "logs" / "rename_last.json", report)


if __name__ == "__main__":
    main()
