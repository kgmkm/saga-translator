"""前作の分析ファイルを現作へ引き継ぐ(シリーズ・連作で訳語と声を固定する)。

同じシリーズの前後編・続編を翻訳するとき、前作の characters/terms を現作の
analysis/ に置いてから分析を始める。エージェントはこれを起点にするので、
話ごとに一人称・口調・訳語がずれない。

    python3 carry_analysis.py <workdir> --from <前作workdir> [--replace]

- 前作1作だけでなく、前作の manifest の series_from を再帰で辿った
  **連鎖全体の和集合**を引き継ぐ（CA-1。前作1作のみだと、それより前の作品に
  しか居ない人物が連鎖の途中で無音で落ちていた）
- 連鎖の欠落（リンク切れ・循環・引き継げなかった項目）は名指しで表示する
- arcs は前作のものをそのまま持ち込まず、引き継ぎ1件に畳む
  (前作のブロック番号のままだと、現作の同じ番号のブロックで誤適用される)
- 現作に既に同じ name_src / term_src があれば現作を優先し、空欄だけ前作で埋める
- --replace で現作側の同種ファイルを引き継ぎ内容で作り直す
- [naming_policy]（命名方針。style は人名、codename はコードネーム）も引き継ぐ
  （現作が未決定のときだけ。P0-5）
- 台帳は term_first_use / ono_used を引き継ぐ。ruby_applied は引き継がない
  （初出ルビは作品単位の既定。G10 / T12）
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
    load_structured,
    load_toml,
    save_toml,
    utf8,
    voice_status_of,
)

CHAR_FIELDS = (
    "name_src",
    "name_ja",
    "aliases",
    "dismissed_variants",
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


# ── 連鎖（series_from の再帰。G22 / CA-1） ─────────────────────────────


def _manifest_of(work: Path) -> dict:
    """manifest（manifest.toml / 旧 manifest.json）を読む。無ければ {}。"""
    try:
        return load_structured(work / "manifest")
    except FileNotFoundError:
        return {}
    except Exception:
        return {}


def _series_chain(prev: Path) -> tuple[list[Path], list[str]]:
    """--from の作品から series_from を再帰で辿り、連鎖（手前 → 過去）を返す。

    各作品の manifest の series_from（ingest --series-from が記録した前作の
    workdir）を辿る。従来は前作1作しか見ず、それより前の作品にしか居ない
    人物が連鎖の途中で無音で落ちていた（実測: 伝播成功率 24%）。

    リンク切れ・循環は警告（欠落を名指し）して穏当に打ち切る。
    戻り値: (ワーク列, 警告行)。
    """
    chain: list[Path] = []
    warns: list[str] = []
    seen: set[str] = set()
    cur = prev
    for _ in range(50):
        chain.append(cur)
        try:
            seen.add(str(cur.resolve()))
        except OSError:
            seen.add(str(cur))
        m = _manifest_of(cur)
        sf = str(m.get("series_from") or "").strip()
        if not sf:
            break
        nxt = Path(sf)
        if not nxt.is_dir():
            warns.append(
                f"[carry] 連鎖の欠落: 『{_prev_title(cur)}』の series_from が指す "
                f"{nxt} が見つかりません（ここで連鎖を打ち切ります）")
            break
        try:
            nkey = str(nxt.resolve())
        except OSError:
            nkey = str(nxt)
        if nkey in seen:
            warns.append(
                f"[carry] 連鎖の循環: {nkey} は既に連鎖内にあります（ここで連鎖を打ち切ります）")
            break
        cur = nxt
    return chain, warns


def _carry_note(title: str, direct_prev: bool = True) -> str:
    """引き継ぎ1件の注記（arcs の at_block="carry" として書く）。"""
    who = "前作" if direct_prev else "連鎖の過去作"
    return (
        f"{who}『{title}』**終了時点**の状態を引き継ぎ。"
        f"{who} arcs の声の変化(一人称・二人称・口調・語彙)は畳み込み済み。"
        "この値を現作の起点とし、変化があれば arcs に追記する"
    )


def _fmt_carry_names(names: list[str], cap: int = 8) -> str:
    """名前一覧を「N件 [a, b, …他k件]」の1行にする（欠落の名指し表示用）。"""
    shown = ", ".join(names[:cap])
    if len(names) > cap:
        shown += f" …他{len(names) - cap}件"
    return f"{len(names)}件 [{shown}]"


def _carry_gap_report(by_source: list[list[dict]], merged: list[dict],
                      cur_before: list[dict], key: str) -> tuple[list[str], list[str]]:
    """連鎖の欠落を名指しする材料を作る（CA-3）。

    - 1つ目: 直接の前作に無く、古い側の作品からだけ引き継いだキー
      （前作1作だけを見ていた従来実装では落ちていた項目）
    - 2つ目: 連鎖にあるのに現作の表へ入らなかった項目の表示名
      （キー欠落など。入っていれば通常は空）
    """
    def _key(it: dict) -> str:
        return str(it.get(key) or "").strip()

    def _label(it: dict) -> str:
        if key == "name_src":
            return str(it.get("name_ja") or it.get("name_src") or "(無名)")
        return str(it.get("term_src") or it.get("term_ja") or "(無名)")

    prev_keys = {_key(it) for it in (by_source[0] if by_source else []) if _key(it)}
    cur_keys = {_key(it) for it in cur_before if _key(it)}
    older_only: list[str] = []
    for items in by_source[1:]:
        for it in items:
            k = _key(it)
            if k and k not in prev_keys and k not in cur_keys and k not in older_only:
                older_only.append(k)
    merged_keys = {_key(it) for it in merged if _key(it)}
    missing: list[str] = []
    for items in by_source:
        for it in items:
            if _key(it) in merged_keys:
                continue
            label = _label(it)
            if label not in missing:
                missing.append(label)
    return older_only, missing


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
        if isinstance(pol, dict) and (str(pol.get("style") or "").strip()
                                      or str(pol.get("codename") or "").strip()):
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
        cur_bits = ", ".join(x for x in (
            f"style={str(cur_pol.get('style') or '')}",
            f"codename={str(cur_pol.get('codename') or '')}" if cur_pol.get("codename") else "",
        ) if x and not x.endswith("="))
        print(f"[carry] 命名方針: 現作で決定済み（{cur_bits}）。"
              "前作の記録からのコピーは行いません")
        return
    style = str(prev_pol.get("style") or "").strip()
    scope = str(prev_pol.get("scope") or "").strip()
    codename = str(prev_pol.get("codename") or "").strip()
    note = str(prev_pol.get("note") or "").strip()
    detail = ", ".join(x for x in (
        f"scope={scope}" if scope else "",
        f"codename={codename}" if codename else "",
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

    - ruby_applied は引き継がない（G10 / T12。初出ルビは作品単位の既定。
      前作の消費済みルビを渡すと現作の初出ルビが「消費済み」に見えて無音で
      落ちる実害があった）。現作自身の消費済みルビは残す
    - term_first_use / ono_used は前作から引き継ぐ（現作に既にある分と和集合。
      キーで重複排除）
    """
    prev_ledger = prev / "_work" / "ledger.toml"
    if not prev_ledger.is_file():
        print("[carry] ledger: 前作に台帳はありません")
        return
    try:
        from ledger import load_ledger, save_ledger
        prev_data = load_ledger(prev)
        cur_data = load_ledger(work)
    except Exception as e:
        print(f"[carry] 警告: 台帳を引き継ぎできませんでした（手動でコピーしてください）: {e}")
        return
    ruby_dropped = len(prev_data.get("ruby_applied") or [])

    def _key(section: str, e: dict):
        if section == "ruby_applied":
            return (str(e.get("term") or ""), str(e.get("reading") or ""))
        if section == "term_first_use":
            return (str(e.get("term") or ""),)
        return (str(e.get("form") or ""), str(e.get("block") or ""), e.get("paragraph"))

    merged: dict = {
        # 前作の ruby_applied は入れない。現作自身の記録だけを残す
        "ruby_applied": list(cur_data.get("ruby_applied") or []),
        "term_first_use": list(prev_data.get("term_first_use") or []),
        "ono_used": list(prev_data.get("ono_used") or []),
    }
    for section in ("ruby_applied", "term_first_use", "ono_used"):
        seen = {_key(section, e) for e in merged[section]}
        for e in cur_data.get(section) or []:
            k = _key(section, e)
            if k not in seen:
                seen.add(k)
                merged[section].append(e)

    if not (merged["ruby_applied"] or merged["term_first_use"] or merged["ono_used"]):
        print("[carry] ledger: 引き継ぐ項目がありません"
              f"（前作の ruby_applied {ruby_dropped}件は作品単位のため除外）")
        return
    try:
        save_ledger(work, merged)
    except Exception as e:
        print(f"[carry] 警告: 台帳を引き継ぎできませんでした（手動でコピーしてください）: {e}")
        return
    try:
        from ledger import ledger_counts
        c = ledger_counts(work)
    except Exception:
        c = None
    counts = f"ルビ{c[0]}件・用語初出{c[1]}件・オノマトペ使用{c[2]}件" if c else "件数不明"
    ruby_part = ""
    if ruby_dropped:
        ruby_part = f"。ruby_applied は作品単位のため引き継がず（前作{ruby_dropped}件を除外）"
    print(f"[carry] ledger: 前作の台帳を引き継ぎました（{counts}{ruby_part}） -> {work / '_work' / 'ledger.toml'}")


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

    chain, chain_warns = _series_chain(prev)
    chain_meta = [(w, _prev_title(w)) for w in chain]
    title = chain_meta[0][1]

    adir = work / "analysis"
    adir.mkdir(parents=True, exist_ok=True)

    def _load_chain_items(kind: str) -> list[list[dict]]:
        """連鎖の各作品から items を読む（近い側が先）。kind は characters / terms。"""
        by_source: list[list[dict]] = []
        for w in chain:
            p = (characters_toml_path(w) if kind == "characters"
                 else w / "analysis" / "terms.toml")
            items: list[dict] = []
            if p.exists():
                try:
                    raw = load_toml(p).get(kind) or []
                except Exception:
                    raw = []
                items = [c for c in raw if isinstance(c, dict)]
            by_source.append(items)
        return by_source

    # 登場人物: 連鎖全体の和集合を近い側優先でマージする（CA-1。前作1作のみ
    # だと、それより前の作品にしか居ない人物が連鎖の途中で無音で落ちていた）
    dst_c = adir / "characters-[ネタバレあり].toml"
    cur_chars: list[dict] = []
    if dst_c.exists() and not args.replace:
        cur_chars = load_toml(dst_c).get("characters") or []
    chars_by_source = _load_chain_items("characters")
    merged_c = cur_chars
    added_c = filled_c = 0
    for idx, items in enumerate(chars_by_source):
        folded = [_fold_arcs(c) for c in items]
        merged_c, a, f = _merge_items(folded, merged_c, "name_src", CHAR_FIELDS,
                                      _carry_note(chain_meta[idx][1], direct_prev=(idx == 0)))
        added_c += a
        filled_c += f
    save_toml(dst_c, {"characters": merged_c})

    # 用語: 同じく連鎖全体の和集合
    dst_t = adir / "terms.toml"
    cur_terms: list[dict] = []
    if dst_t.exists() and not args.replace:
        cur_terms = load_toml(dst_t).get("terms") or []
    terms_by_source = _load_chain_items("terms")
    merged_t = cur_terms
    added_t = filled_t = 0
    for idx, items in enumerate(terms_by_source):
        merged_t, a, f = _merge_items(items, merged_t, "term_src", TERM_FIELDS,
                                      _carry_note(chain_meta[idx][1], direct_prev=(idx == 0)))
        added_t += a
        filled_t += f
    save_toml(dst_t, {"terms": merged_t})

    print(f"[carry] from={prev} title=『{title}』")
    if len(chain) > 1:
        chain_line = " ← ".join(f"『{t}』({w.name})" for w, t in chain_meta)
        print(f"[carry] 連鎖: {chain_line} の{len(chain)}作を和集合で引き継ぎ（series_from 再帰）")
    else:
        print(f"[carry] 連鎖: 前作1作のみ（{prev.name} の manifest に series_from の記録なし）")
    for wmsg in chain_warns:
        print(wmsg)
    print(f"[carry] characters: {len(merged_c)}件 (追加{added_c} / 空欄補完{filled_c}) -> {dst_c}")
    print(f"[carry] terms:      {len(merged_t)}件 (追加{added_t} / 空欄補完{filled_t}) -> {dst_t}")

    # 連鎖の欠落を名指しで表示する（CA-3）
    older_c, missing_c = _carry_gap_report(chars_by_source, merged_c, cur_chars, "name_src")
    older_t, missing_t = _carry_gap_report(terms_by_source, merged_t, cur_terms, "term_src")
    if older_c or older_t:
        parts = []
        if older_c:
            parts.append("characters " + _fmt_carry_names(older_c))
        if older_t:
            parts.append("terms " + _fmt_carry_names(older_t))
        print("[carry] 直接の前作に無く、連鎖の過去作から引き継いだ項目: " + " / ".join(parts))
    if missing_c or missing_t:
        parts = []
        if missing_c:
            parts.append("characters " + _fmt_carry_names(missing_c))
        if missing_t:
            parts.append("terms " + _fmt_carry_names(missing_t))
        print("[carry] 警告: 現作の表に無い項目（引き継げませんでした）: " + " / ".join(parts))
    else:
        print("[carry] 欠落: なし（連鎖の全項目が現作の表にあります）")

    # 命名方針（P0-5）: 前作の [naming_policy] を現作の house_style 候補として引き継ぐ
    _carry_naming_policy(work, prev)

    # 台帳（レビュー提案1）: 前作の消費状態（用語初出・オノマトペ）を継続する。
    # ruby_applied は引き継がない（初出ルビは作品単位の既定。G10 / T12）
    _carry_ledger(work, prev)

    print("[carry] 以降の分析は引き継いだ訳語・声を確定値として扱い、変化だけ arcs に追記すること")


if __name__ == "__main__":
    main()
