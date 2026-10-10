"""analysis TOML の「原文キー」が日本語化されていないか検査する。

背景: 分析サブエージェントが term_src / name_src を日本語表記へ書き換えてしまい、
同じ語のエントリが二重化した（例: term_src="阳光环" と "光A環" が併存）。
原文キーは原文のままが正しい（訳語は term_ja / name_ja 側に書く）。

検出方法:
  1. 原文キーの重複（同じ term_ja に対して複数の term_src がある）
  2. 原文キーに、日本語新字体だけに現れる字が含まれる（原文が簡体字なら誤り）
     例: 光A環（陽・環は日本語）／阳光环（简体字）が正しい

使い方:
    python3 check_src_keys.py <work_dir> [...] [--fix]
    python3 check_src_keys.py <work_dir> --delta <差分TOML> [...]   # 差分の書式検査
    --fix は「重複エントリのうち、原文キーが日本語化されている側」を削除する。
    削除の判断がつかない場合は report のみ。
    --delta は analyze-commit の入力（差分 TOML）を事前検査する（G51 / F9-P0-1）。
    characters_delta / terms_delta の未知キーを plot_delta と同型に拒否する。
    相対パスは work 相対で解決する（F4-C4 / F5-H1）。
"""
from __future__ import annotations

import argparse
import glob
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _lib import load_characters, load_terms, load_toml  # noqa: E402

# 日本語新字体にだけ現れる字（簡体字側は別字）。原文キーにこれが入っていたら怪しい。
JA_ONLY = set("陽環嵐聖輝戦姫説実発現見開間問東車馬鳥魚門楼涙鈴師缆専")  # 据・結は日本語でも使うため除外

# 日本語化の疑いが特に強い語（過去に実害が出たもの）
KNOWN_BAD = {"光A環": "阳光环", "日冕嵐": "日冕风暴"}


# ── 差分 TOML の書式検査（G51 / F9-P0-1。plot_delta と同型の未知キー拒否） ──
#
# analyze-commit は characters_delta / terms_delta の未知キーを素通しし、
# 誤ったキー名がそのまま分析 TOML のキーになって TOML を破損させる
# （実害: updates に `卵巢 = "卵巣"` と書いて次コマンドから読めなくなった）。
# ここで analyze-commit の前に未知キーを検出して止める。plot_delta は
# saga.py の PLOT_DELTA_KEYS と同じ許容キーで検査する。

#: characters_delta の項目・updates・new_fields で許されるフィールドキー。
CHARACTERS_FIELD_KEYS = frozenset({
    "name_src", "name_ja", "ruby", "role", "first_person", "second_person",
    "speech_style", "vocab", "catchphrase", "appearance", "relationships",
    "address", "arcs", "proposed", "aliases", "voice_status", "voice_note",
    "dismissed_variants", "variant_candidates", "speaker_candidate",
    "dismiss_variant", "dismiss_reason", "note", "tone",
})
#: terms_delta の項目・updates・new_fields で許されるフィールドキー。
TERMS_FIELD_KEYS = frozenset({
    "term_src", "term_ja", "ruby", "memo", "aliases", "voice_status",
    "voice_note", "arcs", "proposed", "dismissed_variants",
})
#: 項目（配列要素）自体で許される構造キーを加えたもの。
CHARACTERS_DELTA_KEYS = CHARACTERS_FIELD_KEYS | {
    "match_key", "match_value", "updates", "new_fields"}
TERMS_DELTA_KEYS = TERMS_FIELD_KEYS | {
    "match_key", "match_value", "updates", "new_fields"}
#: plot_delta エントリの許容キー（saga.py の PLOT_DELTA_KEYS と同型）。
PLOT_DELTA_KEYS = frozenset({"block", "text", "replace", "updates", "new_fields"})


def _check_delta_item(i: int, item, key: str,
                      allowed: frozenset[str], field_keys: frozenset[str],
                      problems: list[str]) -> None:
    """delta 配列の1項目を検査する（必須キー・未知キー・型）。"""
    where = f"{key}[{i}]"
    if not isinstance(item, dict):
        problems.append(f"{where} はテーブル（配列ofテーブル）であるべきです")
        return
    for rk in ("match_key", "match_value"):
        if not (isinstance(item.get(rk), str) and item[rk].strip()):
            problems.append(f"{where} に必須キー `{rk}`（文字列）がありません")
    unknown = [k for k in item if k not in allowed]
    if unknown:
        problems.append(f"{where} に未知のキー `{unknown[0]}` があります"
                        f"（許容: {' / '.join(sorted(allowed))}）")
    for sub in ("updates", "new_fields"):
        d = item.get(sub)
        if d is None:
            continue
        if not isinstance(d, dict):
            problems.append(f"{where}.{sub} はテーブルであるべきです")
            continue
        bad = [k for k in d if k not in field_keys]
        if bad:
            problems.append(
                f"{where}.{sub} に未知のキー `{bad[0]}` があります"
                f"（許容: {' / '.join(sorted(field_keys))}）。"
                "キー名の誤りは分析 TOML を破損させます")
    # よくある型の誤り（aliases は文字列配列。dismiss_variant は相手の原語名）
    for loc, holder in (("", item), ("updates", item.get("updates")),
                        ("new_fields", item.get("new_fields"))):
        if not isinstance(holder, dict):
            continue
        at = f"{where}." + (f"{loc}." if loc else "")
        if "aliases" in holder and (not isinstance(holder["aliases"], list)
                                    or not all(isinstance(x, str) for x in holder["aliases"])):
            problems.append(f"{at}aliases は文字列の配列であるべきです")
        if "dismiss_variant" in holder and (
                not isinstance(holder["dismiss_variant"], str)
                or not holder["dismiss_variant"].strip()):
            problems.append(f"{at}dismiss_variant は相手の name_src（空でない文字列）であるべきです")


def check_delta(path: Path) -> tuple[list[str], list[str]]:
    """差分 TOML を検査する（G51 / F9-P0-1 / F8-DX-1 相当）。

    戻り値: (problems, notes)。problems が空なら OK。
    """
    problems: list[str] = []
    notes: list[str] = []
    try:
        data = load_toml(path)
    except Exception as e:  # TomlSyntaxError / OSError 等
        return [f"delta を読めません: {e}"], notes
    if not isinstance(data, dict):
        return ["差分は TOML テーブルであるべきです"], notes

    block = data.get("block")
    if not isinstance(block, str) or not block.strip():
        problems.append('必須キー `block`（文字列。例: block = "ch001/b1"）がありません')
        block = ""
    else:
        block = block.strip()

    known_top = {"block", "no_changes", "characters_delta", "terms_delta", "plot_delta"}
    unknown_top = sorted(k for k in data if k not in known_top)
    if unknown_top:
        notes.append("トップレベルの未知キーは無視されます: " + " / ".join(unknown_top))

    for key in ("characters_delta", "terms_delta", "plot_delta"):
        if not isinstance(data.get(key, []), list):
            problems.append(f"`{key}` は配列（[[{key}]]）であるべきです")

    for i, c in enumerate(data.get("characters_delta", []) or []):
        _check_delta_item(i, c, "characters_delta", CHARACTERS_DELTA_KEYS,
                          CHARACTERS_FIELD_KEYS, problems)
    for i, t in enumerate(data.get("terms_delta", []) or []):
        _check_delta_item(i, t, "terms_delta", TERMS_DELTA_KEYS,
                          TERMS_FIELD_KEYS, problems)

    for i, p in enumerate(data.get("plot_delta", []) or []):
        where = f"plot_delta[{i}]"
        if not isinstance(p, dict):
            problems.append(f"{where} はテーブル（配列ofテーブル）であるべきです")
            continue
        if not (isinstance(p.get("text"), str) and p["text"].strip()):
            problems.append(f"{where} に必須キー `text`（文字列）がありません")
        pb = p.get("block")
        if not isinstance(pb, str) or pb.strip() != block:
            problems.append(f"{where} の `block` が delta の block と一致しません"
                            f"（要: {block!r}）")
        if "replace" in p and not isinstance(p["replace"], bool):
            problems.append(f"{where}.replace は true / false（boolean）であるべきです")
        unknown = [k for k in p if k not in PLOT_DELTA_KEYS]
        if unknown:
            problems.append(f"{where} に未知のキー `{unknown[0]}` があります"
                            f"（許容: {' / '.join(sorted(PLOT_DELTA_KEYS))}）")
        for sub in ("updates", "new_fields"):
            d = p.get(sub)
            if d is None:
                continue
            if not isinstance(d, dict):
                problems.append(f"{where}.{sub} はテーブルであるべきです")
                continue
            bad = [k for k in d if k not in ("block", "text")]
            if bad:
                problems.append(f"{where}.{sub} に未知のキー `{bad[0]}` があります"
                                "（許容: block / text）")

    has = any(len(data.get(k) or []) > 0
              for k in ("characters_delta", "terms_delta", "plot_delta"))
    if not has and data.get("no_changes") is not True:
        problems.append("差分が空です。変更が無い場合は `no_changes = true` を明示してください")
    return problems, notes


def resolve_delta_path(work: Path, arg: str) -> Path:
    """--delta のパスを解決する（F4-C4 / F5-H1: work 相対を許す）。

    絶対パスはそのまま。相対パスは work 相対 → 実行ディレクトリ相対 の順で
    実在を確認して返す（どちらにも無ければ work 相対のパスを返し、
    呼び出し側がエラー表示に使う）。
    """
    p = Path(arg)
    if p.is_absolute():
        return p
    cand = Path(work) / p
    if cand.is_file():
        return cand
    if p.is_file():
        return p
    return cand


def run_delta_checks(works: list[Path], deltas: list[str]) -> int:
    """--delta の検査を実行する。戻り値: 0=OK / 1=問題あり / 2=ファイル不在。"""
    rc = 0
    for arg in deltas:
        resolved = None
        for w in works:
            cand = resolve_delta_path(w, arg)
            if cand.is_file():
                resolved = cand
                break
        if resolved is None:
            resolved = resolve_delta_path(works[0], arg)
        print(f"[delta-check] {resolved}")
        if not resolved.is_file():
            print(f"[delta-check] エラー: delta ファイルが見つかりません: {resolved}",
                  file=sys.stderr)
            rc = max(rc, 2)
            continue
        problems, notes = check_delta(resolved)
        for n in notes:
            print(f"[delta-check]   NOTE {n}")
        if problems:
            for p in problems:
                print(f"[delta-check]   NG  {p}")
            print(f"[delta-check]   → {len(problems)}件の問題。"
                  "analyze-commit の前に修正してください")
            rc = max(rc, 1)
        else:
            print("[delta-check]   OK  （未知キー・必須キーに問題なし）")
    return rc


def entries_of(path: Path):
    d = load_toml(path)
    for key in ("terms", "characters"):
        if key in d:
            return key, d[key]
    return None, []


def main() -> int:
    ap = argparse.ArgumentParser(description="analysis TOML 原文キー検査")
    ap.add_argument("works", nargs="+", metavar="WORK_DIR",
                    help="作品ディレクトリ（_saga/ 内部またはその親）")
    ap.add_argument("--fix", action="store_true",
                    help="重複エントリのうち日本語化側を削除する（報告のみで実際の削除はしない）")
    ap.add_argument("--delta", action="append", default=None, metavar="差分TOML",
                    help="差分 TOML の書式検査（未知キー・必須キー。相対パスは work 相対で解決。複数指定可）")
    args = ap.parse_args()

    total = 0
    for w in args.works:
        work = Path(w)
        if not work.is_dir():
            print(f"ディレクトリが見つかりません: {work}", file=sys.stderr)
            return 2
        for f in sorted(work.glob("analysis/*.toml")):
            key, items = entries_of(f)
            if not items:
                continue
            src_key = "term_src" if key == "terms" else "name_src"
            ja_key = "term_ja" if key == "terms" else "name_ja"

            # 1) 同じ訳語に対して複数の原文キー（重複エントリ）
            by_ja: dict[str, list[int]] = {}
            for i, x in enumerate(items):
                ja = x.get(ja_key)
                if ja:
                    by_ja.setdefault(ja, []).append(i)
            for ja, idxs in by_ja.items():
                if len(idxs) > 1:
                    rel = f.relative_to(work) if f.is_relative_to(work) else f
                    print(f"===== {rel}  重複: {ja} ({len(idxs)}件)")
                    for i in idxs:
                        print(f"   - {src_key} = \"{items[i].get(src_key)}\"")
                    total += len(idxs) - 1
                    if args.fix:
                        # 日本語化されている側（JA_ONLY を含む src）を削除
                        drop = [i for i in idxs if any(c in JA_ONLY for c in str(items[i].get(src_key, "")))]
                        if drop and len(drop) < len(idxs):
                            print(f"   -> 削除候補: {[items[i].get(src_key) for i in drop]}")
                            print("   ※ --fix はこのツールでは実行しません。手動で該当エントリを削除してください。")

            # 2) 既知の誤り
            for i, x in enumerate(items):
                s = str(x.get(src_key, ""))
                for bad, good in KNOWN_BAD.items():
                    if s == bad:
                        rel = f.relative_to(work) if f.is_relative_to(work) else f
                        print(f"===== {rel}  原文キーが日本語化: {src_key}=\"{bad}\" → \"{good}\" が正")
                        total += 1

            # 3) 原文キーに日本語新字体だけの字が混入していないか（一括置換事故の検出）
            for i, x in enumerate(items):
                s = str(x.get(src_key, ""))
                bad = [c for c in s if c in JA_ONLY]
                if bad:
                    rel = f.relative_to(work) if f.is_relative_to(work) else f
                    print(f"===== {rel}  原文キーに日本語字: {src_key}=\"{s}\"  字={bad}")
                    total += 1
    print(f"\n合計 {total} 件の疑い")
    rc = 1 if total else 0
    if args.delta:
        rc = max(rc, run_delta_checks([Path(w) for w in args.works], args.delta))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())