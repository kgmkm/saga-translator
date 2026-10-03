"""進捗の実体検査（saga-translator 鉄則0）。

chapters_meta の各 block について
  - 分析の実体: analysis/plot-[ネタバレあり].toml に block = "chNNN/bN" があるか
  - 翻訳の実体: output/chNNN/bN_ja.md があるか
を突き合わせ、フラグのみ／実体のみ／番号の飛び（HOLE）／末尾未達（MISSING-TAIL）を出す。

使い方:
  python3 verify_progress.py [--work <path>] [<path>]
  path は _saga/ を含むか自身が作品ディレクトリ。省略時はカレントディレクトリを走査。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import direction, load_chapters_meta, load_toml, target_suffix  # noqa: E402

PLOT_RE = re.compile(r'^block = "([^"]+)"', re.M)


def find_works(root: Path) -> list[Path]:
    """作品ディレクトリの一覧（_saga/ があれば中を見る）。"""
    if (root / "_saga").is_dir():
        root = root / "_saga"
    def _has_manifest(p: Path) -> bool:
        return (p / "manifest.toml").is_file() or (p / "manifest.json").is_file()
    if root.name == "_saga" or _has_manifest(root):
        if _has_manifest(root):
            return [root]
    return sorted(
        p for p in root.iterdir()
        if p.is_dir() and _has_manifest(p)
    )


def holes(nums: list[int]) -> list[tuple[int, int]]:
    """欠番区間を返す。"""
    out = []
    for a, b in zip(nums, nums[1:]):
        if b != a + 1:
            out.append((a + 1, b - 1))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="進捗実体検査")
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help="作品ディレクトリまたは親ディレクトリ（省略時はカレント）")
    args = ap.parse_args()

    roots = [Path(a) for a in (args.paths or ["."])]
    works: list[Path] = []
    for r in roots:
        works.extend(find_works(r))
    if not works:
        print("作品ディレクトリが見つからない", file=sys.stderr)
        return 1

    problems = 0
    for w in works:
        meta_p = w / "chapters_meta.toml"
        if not meta_p.is_file():
            # JSON形式も試す（後方互換）
            meta_p = w / "chapters_meta.json"
        if not meta_p.is_file():
            print(f"[{w.name}] chapters_meta なし", file=sys.stderr)
            problems += 1
            continue
        meta = load_chapters_meta(w)
        p_plot = w / "analysis" / "plot-[ネタバレあり].toml"
        p_plot_json = w / "analysis" / "plot-[ネタバレあり].json"
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

        _, dst = direction(w)
        suffix = target_suffix(dst)

        print(f"=== {w.name} ({meta.get('work', {}).get('title', '')}) ===")
        for ch in meta.get("chapters", []):
            cid = ch["id"]
            ja_dir = w / "output" / cid
            for b in ch.get("blocks", []):
                bid = b["id"]
                key = f"{cid}/{bid}"
                ana = key in plot_blocks
                tr = (ja_dir / f"{bid}{suffix}.md").is_file()
                a_flag = bool(b.get("analysis_started_at") or b.get("analysis_committed_at") or b.get("analysis_done_at"))
                t_flag = bool(b.get("translation_done_at"))
                bits = []
                if ana:
                    bits.append("ana✓")
                if tr:
                    bits.append("tr✓")
                if a_flag and not ana:
                    bits.append("ana-flag-only")
                if t_flag and not tr:
                    bits.append("tr-flag-only")
                if ana and not a_flag:
                    bits.append("ana-flag-missing")
                if tr and not t_flag:
                    bits.append("tr-flag-missing")
                if not ana or not tr:
                    print(f"  {key:12s} " + " ".join(bits) if bits else f"  {key:12s} (なし)")
            # 章単位の連続性
            all_b = [b["id"] for b in ch.get("blocks", [])]
            nums = [int(re.sub(r"\D", "", x)) for x in all_b]
            h = holes(nums)
            ana_nums = [int(re.sub(r"\D", "", k.split("/")[1])) for k in plot_blocks if k.startswith(cid + "/")]
            tr_nums = [int(re.sub(r"\D", "", p.stem)) for p in ja_dir.glob(f"b*{suffix}.md")] if ja_dir.is_dir() else []
            print(f"  [{cid}] expected b{min(nums)}..b{max(nums)} ({len(nums)}) | ana={len(ana_nums)} | tr={len(tr_nums)}")
            if h:
                print(f"  [{cid}] HOLE in block list: {h}")
                problems += 1
            miss_a = sorted(set(nums) - set(ana_nums))
            miss_t = sorted(set(nums) - set(tr_nums))
            if miss_a:
                print(f"  [{cid}] MISSING analysis: b{miss_a[0]}..b{miss_a[-1]} ({len(miss_a)})")
            if miss_t:
                print(f"  [{cid}] MISSING translation: b{miss_t[0]}..b{miss_t[-1]} ({len(miss_t)})")
    print()
    print(f"works={len(works)}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())