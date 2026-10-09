"""前作の分析ファイルを現作へ引き継ぐ(シリーズ・連作で訳語と声を固定する)。

同じシリーズの前後編・続編を翻訳するとき、前作の characters/terms を現作の
analysis/ に置いてから分析を始める。エージェントはこれを起点にするので、
話ごとに一人称・口調・訳語がずれない。

    python3 carry_analysis.py <workdir> --from <前作workdir> [--replace]

- arcs は前作のものをそのまま持ち込まず、引き継ぎ1件に畳む
  (前作のブロック番号のままだと、現作の同じ番号のブロックで誤適用される)
- 現作に既に同じ name_src / term_src があれば現作を優先し、空欄だけ前作で埋める
- --replace で現作側の同種ファイルを引き継ぎ内容で作り直す
- [naming_policy]（命名方針）も引き継ぐ（現作が未決定のときだけ。P0-5）
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import (  # noqa: E402
    VOICE_STATUS_DEFAULT,
    atomic_write_text,
    characters_toml_path,
    dump_toml,
    load_toml,
    save_toml,
    utf8,
    voice_status_of,
)

CHAR_FIELDS = (
    "name_src",
    "name_ja",
    "aliases",
    "ruby",
    "role",
    "first_person",
    "second_person",
    "speech_style",
    "vocab",
    "catchphrase",
    "voice_status",
    "voice_note",
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
    """前作の項目を現作へ足す。戻り値: (統合後の列, 追加数, 空欄補完数)。

    voice_status は unconfirmed（＝未確定）を空欄の明示として扱い、前作の
    確定状態だけが埋める。aliases / voice_note は既存の他欄と同じ扱い（P0-4）。
    """
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
                if f == "voice_status":
                    # unconfirmed は「未確定」＝空欄の明示。前作の確定状態で埋める。
                    if (voice_status_of(cur) == VOICE_STATUS_DEFAULT
                            and voice_status_of(pi) != VOICE_STATUS_DEFAULT):
                        cur[f] = pi[f]
                        filled += 1
                    continue
                if not cur.get(f) and pi.get(f):
                    cur[f] = pi[f]
                    filled += 1
            continue
        item = {f: pi.get(f, "") for f in fields}
        if not isinstance(item.get("aliases"), list):
            item["aliases"] = []
        if not str(item.get("voice_status") or "").strip():
            # 空文字列の印をやめ、既定を明示する（P0-4）
            item["voice_status"] = VOICE_STATUS_DEFAULT
        item["arcs"] = [{"at_block": "carry", "note": note}]
        order.append(k)
        by_key[k] = item
        added += 1
    return [by_key[k] for k in order], added, filled


# ── 命名方針（P0-5） ──────────────────────────────────────────────


def _find_naming_policy(work: Path) -> tuple[dict | None, Path | None]:
    """house_style.toml（共通探索先）から [naming_policy] を探す。

    戻り値: (方針, 見つかったファイル)。無ければ (None, None)。
    """
    from vocab import house_style_paths

    for p in house_style_paths(work):
        try:
            data = load_toml(p)
        except Exception:
            continue
        pol = data.get("naming_policy")
        if isinstance(pol, dict) and str(pol.get("style") or "").strip():
            return pol, p
    return None, None


def _naming_target_path(work: Path) -> Path:
    """引き継ぎ先の house_style.toml。

    作品側（work/_house）が既にあればそれを使う。無ければ、スキーマ文書の
    置き場である親側（work.parent/_house）に作る。
    """
    work_side = work / "_house" / "house_style.toml"
    if work_side.is_file():
        return work_side
    return work.parent / "_house" / "house_style.toml"


def _write_naming_policy(path: Path, policy: dict) -> None:
    """house_style.toml に [naming_policy] を書く（既存の内容は保つ）。

    - ファイルが無い: 最小の house_style として新規作成する
    - [naming_policy] が無い: 末尾に追記する（既存の本文・コメントを変えない）
    - 既にある: その欄だけを置き換える
    """
    if not path.is_file():
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        atomic_write_text(path, dump_toml({
            "version": 1,
            "created_at": now,
            "updated_at": now,
            "rules": [],
            "naming_policy": dict(policy),
        }))
        return
    data = load_toml(path)
    if "naming_policy" in data:
        data["naming_policy"] = dict(policy)
        save_toml(path, data)
        return
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    block = dump_toml({"naming_policy": dict(policy)}).rstrip() + "\n"
    atomic_write_text(path, text + "\n" + block)


def _carry_naming_policy(work: Path, prev: Path) -> None:
    """前作の [naming_policy] を現作へ引き継ぐ（P0-5）。

    - 前作に記録が無ければ何もしない
    - 現作で決定済みならコピーしない（現作を優先）
    - 現作が未決定なら「前作の方針: …（このまま進めるか確認）」を表示したうえで、
      現作の house_style にコピーする（人が確認・修正できるようにするため）
    """
    prev_pol, _ = _find_naming_policy(prev)
    cur_pol, _ = _find_naming_policy(work)
    if prev_pol is None:
        print("[carry] 命名方針: 前作に [naming_policy] の記録はありません")
        return
    if cur_pol is not None:
        print(f"[carry] 命名方針: 現作で決定済み（style={str(cur_pol.get('style') or '')}）。"
              "前作の記録からのコピーは行いません")
        return
    style = str(prev_pol.get("style") or "").strip()
    scope = str(prev_pol.get("scope") or "").strip()
    note = str(prev_pol.get("note") or "").strip()
    detail = ", ".join(x for x in (
        f"scope={scope}" if scope else "",
        f"note={note}" if note else "",
    ) if x)
    suffix = f"（{detail}）" if detail else ""
    print(f"[carry] 前作の方針: {style}{suffix}（このまま進めるか確認）")
    target = _naming_target_path(work)
    try:
        _write_naming_policy(target, prev_pol)
    except Exception as e:
        print(f"[carry] 警告: 命名方針を house_style へコピーできませんでした"
              f"（手動で記録してください）: {e}")
        return
    print(f"[carry] 命名方針を現作の house_style へコピーしました: {target}")


# ── 台帳（ブロック横断状態の外部化。レビュー提案1） ──────────────────────


def _carry_ledger(work: Path, prev: Path) -> None:
    """前作の台帳（_work/ledger.toml）を現作へ引き継ぐ（連作で消費状態を継続）。

    最小実装: 前作にファイルがあればコピーし、1行表示する。無ければその旨を1行。
    """
    prev_ledger = prev / "_work" / "ledger.toml"
    if not prev_ledger.is_file():
        print("[carry] ledger: 前作に台帳はありません")
        return
    dst = work / "_work" / "ledger.toml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        atomic_write_text(dst, prev_ledger.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[carry] 警告: 台帳を引き継ぎできませんでした（手動でコピーしてください）: {e}")
        return
    try:
        from ledger import ledger_counts
        c = ledger_counts(work)
    except Exception:
        c = None
    counts = f"ルビ{c[0]}件・用語初出{c[1]}件・オノマトペ使用{c[2]}件" if c else "件数不明"
    print(f"[carry] ledger: 前作の台帳を引き継ぎました（{counts}） -> {dst}")


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

    # 命名方針（P0-5）: 前作の [naming_policy] を現作の house_style 候補として引き継ぐ
    _carry_naming_policy(work, prev)

    # 台帳（レビュー提案1）: 前作の消費状態（ルビ・用語初出・オノマトペ）を継続する
    _carry_ledger(work, prev)

    print("[carry] 以降の分析は引き継いだ訳語・声を確定値として扱い、変化だけ arcs に追記すること")


if __name__ == "__main__":
    main()
