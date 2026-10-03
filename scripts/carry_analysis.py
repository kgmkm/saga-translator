"""前作の分析ファイルを現作へ引き継ぐ(シリーズ・連作で訳語と声を固定する)。

同じシリーズの前後編・続編を翻訳するとき、前作の characters/terms を現作の
analysis/ に置いてから分析を始める。エージェントはこれを起点にするので、
話ごとに一人称・口調・訳語がずれない。

    python3 carry_analysis.py <workdir> --from <前作workdir> [--replace]

- arcs は前作のものをそのまま持ち込まず、引き継ぎ1件に畳む
  (前作のブロック番号のままだと、現作の同じ番号のブロックで誤適用される)
- 現作に既に同じ name_src / term_src があれば現作を優先し、空欄だけ前作で埋める
- --replace で現作側の同種ファイルを引き継ぎ内容で作り直す
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    characters_toml_path,
    load_toml,
    save_toml,
    utf8,
)

CHAR_FIELDS = (
    "name_src",
    "name_ja",
    "ruby",
    "role",
    "first_person",
    "second_person",
    "speech_style",
    "vocab",
    "catchphrase",
    "appearance",
    "relationships",
    "address",
    "proposed",
)

TERM_FIELDS = ("term_src", "term_ja", "ruby", "memo")


def _prev_title(prev: Path) -> str:
    t = prev / "analysis" / "titles.toml"
    if t.exists():
        try:
            _tt = load_toml(t)
            v = ((_tt.get("title_ja") or _tt.get("title_zh")) or "").strip()
        except Exception:
            v = ""
        if v:
            return v
    return prev.name


def _fold_arcs(item: dict) -> dict:
    """arcs の上書きを基底の欄へ畳み、作品終了時点の状態を作る。

    これをやらないと、前作で覚醒した人物(例: 一人称が僕→伶)が
    現作で前作序盤の声のまま引き継がれてしまう。
    """
    out = dict(item)
    for arc in item.get("arcs") or []:
        if not isinstance(arc, dict):
            continue
        for k, v in arc.items():
            if k in ("at_block", "note"):
                continue
            if v:
                out[k] = v
    return out


def _merge_items(prev_items: list, cur_items: list, key: str, fields: tuple, note: str) -> tuple[list, int, int]:
    """前作の項目を現作へ足す。戻り値: (統合後の列, 追加数, 空欄補完数)。"""
    order: list = []
    by_key: dict = {}
    for it in cur_items:
        k = it.get(key)
        if k and k not in by_key:
            order.append(k)
            by_key[k] = it
    added = filled = 0
    for pi in prev_items:
        k = pi.get(key)
        if not k:
            continue
        if k in by_key:
            cur = by_key[k]
            for f in fields:
                if f == key:
                    continue
                if not cur.get(f) and pi.get(f):
                    cur[f] = pi[f]
                    filled += 1
            continue
        item = {f: pi.get(f, "") for f in fields}
        item["arcs"] = [{"at_block": "carry", "note": note}]
        order.append(k)
        by_key[k] = item
        added += 1
    return [by_key[k] for k in order], added, filled


def main() -> None:
    utf8()
    ap = argparse.ArgumentParser(description="前作の分析を現作へ引き継ぐ")
    ap.add_argument("work")
    ap.add_argument("--from", dest="prev", required=True, help="前作のworkdir")
    ap.add_argument("--replace", action="store_true", help="現作側の同種ファイルを作り直す")
    args = ap.parse_args()

    work = Path(args.work)
    prev = Path(args.prev)
    if not prev.exists():
        raise SystemExit(f"前作ディレクトリがありません: {prev}")
    if not work.exists():
        raise SystemExit(f"作品ディレクトリがありません: {work}")

    title = _prev_title(prev)
    note = (
        f"前作『{title}』**終了時点**の状態を引き継ぎ。"
        "前作 arcs の声の変化(一人称・二人称・口調・語彙)は畳み込み済み。"
        "この値を現作の起点とし、変化があれば arcs に追記する"
    )

    adir = work / "analysis"
    adir.mkdir(parents=True, exist_ok=True)

    # 登場人物
    cp = characters_toml_path(prev)
    prev_chars = load_toml(cp).get("characters") or [] if cp.exists() else []
    prev_chars = [_fold_arcs(c) for c in prev_chars]
    dst_c = adir / "characters-[ネタバレあり].toml"
    cur_chars = []
    if dst_c.exists() and not args.replace:
        cur_chars = load_toml(dst_c).get("characters") or []
    merged_c, added_c, filled_c = _merge_items(prev_chars, cur_chars, "name_src", CHAR_FIELDS, note)
    save_toml(dst_c, {"characters": merged_c})

    # 用語
    tp = prev / "analysis" / "terms.toml"
    prev_terms = load_toml(tp).get("terms") or [] if tp.exists() else []
    dst_t = adir / "terms.toml"
    cur_terms = []
    if dst_t.exists() and not args.replace:
        cur_terms = load_toml(dst_t).get("terms") or []
    merged_t, added_t, filled_t = _merge_items(prev_terms, cur_terms, "term_src", TERM_FIELDS, note)
    save_toml(dst_t, {"terms": merged_t})

    print(f"[carry] from={prev} title=『{title}』")
    print(f"[carry] characters: {len(merged_c)}件 (追加{added_c} / 空欄補完{filled_c}) -> {dst_c}")
    print(f"[carry] terms:      {len(merged_t)}件 (追加{added_t} / 空欄補完{filled_t}) -> {dst_t}")
    print("[carry] 以降の分析は引き継いだ訳語・声を確定値として扱い、変化だけ arcs に追記すること")


if __name__ == "__main__":
    main()
