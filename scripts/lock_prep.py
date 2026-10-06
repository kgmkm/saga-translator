#!python3
"""lock_prep.py — 初期訳語ロック：固有名詞・自称二人称・非JIS字の候補を機械集計し、
固有名の扱い（命名方針）の一覧を出す。

Usage:
    python3 scripts/lock_prep.py <work> [--output-md <path>] [--output-toml <path>] [--top-n N]

入力: <work>/manifest.toml, chapters_meta.toml, chapters/chNNN.paras.toml
      <work>/_house/house_style.toml / <work.parent>/_house/house_style.toml (任意。
      [naming_policy] は命名方針として表示する)
      <work>/analysis/characters-[ネタバレあり].toml / terms.toml (任意)
出力: _work/lock_prep.md / lock_prep.toml (デフォルト)
"""
from __future__ import annotations

import re
import sys
from argparse import ArgumentParser, Namespace
from collections import Counter
from pathlib import Path

# ── プロジェクトスクリプト ──────────────────────────────────────────
_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

from _lib import (
    direction,
    dump_toml,
    find_non_jis_han,
    load_chapters_meta,
    load_characters,
    load_paras,
    load_terms,
    LANG_PACKS,
    load_toml,
    atomic_write_text,
)

# ── 定数 ──────────────────────────────────────────────────────────

# zh→ja 用：簡体字で現れる自称・二人称パターン
# 注: 多字語を先に置く（「奴家」を先に、「奴」など部分マッチを避ける）
ZH_SELF_PRONOUNS = re.compile(
    r"奴家|妾身|寡人|在下|小生|贫道|洒家|人家|自己|自身|"
    r"本(?:人|王|宫|座|大爺|小姐|公子|少爺|姑娘)|"
    r"[我奴咱俺][们]?|朕|某"
)
ZH_SECOND_PERSON = re.compile(
    r"[你尔汝卿][们]?|您|阁下|足下|兄台|道友|先生|小姐|夫人|陛下|殿下"
)

# ja→zh 用：日本語の自称
JA_SELF_PRONOUNS = re.compile(
    r"私|わたくし|あたし|俺|俺様|僕|ぼく|我[々]?|我が|吾[々]?|吾輩|"
    r"小生|拙者|それがし|身共|自分|こちら|あっし|うち|おいら|わし|わっち"
)
JA_SECOND_PERSON = re.compile(
    r"あなた|あんた|君|きみ|お前|おまえ|てめえ|貴様|貴殿|貴方|"
    r"そちら|そっち|お宅|旦那|主人|先生|先輩|君たち"
)

# 2〜6字の漢字連続（固有名詞候補抽出用）
# 非漢字（句読点、英数、空白など）で分割し、各断片から漢字ランを拾う
CJK_RUN = re.compile(r"[\u4e00-\u9fff]{2,6}")

# 非漢字で区切ってから CJK_RUN でマッチするための分割文字
_NON_HAN_SPLIT = re.compile(r"[^\u4e00-\u9fff]+")


def _load_house_style(work: Path) -> dict | None:
    """house_style.toml を共通探索先から読む（任意。#19 の house_style_paths）。

    探索先は `<work>/_house/house_style.toml` → `<work.parent>/_house/house_style.toml`
    （prepare / commit と同じ基準）。無ければ None。
    """
    from vocab import house_style_paths

    for p in house_style_paths(work):
        try:
            return load_toml(p)
        except Exception:
            return None
    return None


def _load_all_paras(work: Path, meta: dict) -> list[tuple[str, int, str]]:
    """全段落を (章ID, ブロック番号, 段落番号, テキスト) の形式で読み込む。"""
    items: list[tuple[str, int, str]] = []
    for ch in meta.get("chapters", []):
        cid: str = ch.get("id", "")
        if not cid:
            continue
        try:
            data = load_paras(work, cid)
        except FileNotFoundError:
            continue
        paras = data.get("paras", [])
        for p in paras:
            text = (p.get("text") or "").strip()
            if text:
                # n が整数出ない場合もあるので fallback
                pn = p.get("n", 0)
                items.append((cid, pn, text))
    return items


def _first_appearance(
    word: str, paras: list[tuple[str, int, str]]
) -> str | None:
    """語の最初の出現を `chXXX/bY [Pn]` 形式で返す。なければ None。"""
    for cid, pn, text in paras:
        if word in text:
            return f"{cid} [P{pn}]"
    return None


def _gather_proper_nouns(
    paras: list[tuple[str, int, str]],
    src: str,
    top_n: int,
) -> list[dict]:
    """2〜6字漢字連続で top_n 件の頻出語を返す。既存分析ファイルは呼び出し側でマージ。"""
    counter: Counter[str] = Counter()
    for _cid, _pn, text in paras:
        # 非漢字で分割 → 各断片は漢字のみのランになる
        for fragment in _NON_HAN_SPLIT.split(text):
            fragment = fragment.strip()
            if len(fragment) < 2:
                continue
            # 2〜6字のスライディングウィンドウで全ての部分文字列をカウント
            max_len = min(6, len(fragment))
            for i in range(len(fragment)):
                for j in range(2, max_len + 1):
                    if i + j <= len(fragment):
                        counter[fragment[i:i+j]] += 1
    # 頻度でソート、top_n 件
    results: list[dict] = []
    for word, freq in counter.most_common(top_n):
        if freq < 3:
            break
        first = _first_appearance(word, paras)
        results.append({
            "word": word,
            "freq": freq,
            "first": first or "",
        })
    return results


def _gather_pronouns(
    paras: list[tuple[str, int, str]],
    src: str,
    top_n: int,
) -> list[dict]:
    """自称・二人称の出現を集計。src が 'zh' なら中国語パターン、'ja' なら日本語パターン。"""
    if src == "ja":
        self_re = JA_SELF_PRONOUNS
        second_re = JA_SECOND_PERSON
    else:
        self_re = ZH_SELF_PRONOUNS
        second_re = ZH_SECOND_PERSON

    self_counter: Counter[str] = Counter()
    second_counter: Counter[str] = Counter()
    self_first: dict[str, str] = {}
    second_first: dict[str, str] = {}

    for cid, pn, text in paras:
        for m in self_re.finditer(text):
            w = m.group()
            self_counter[w] += 1
            if w not in self_first:
                self_first[w] = f"{cid} [P{pn}]"
        for m in second_re.finditer(text):
            w = m.group()
            second_counter[w] += 1
            if w not in second_first:
                second_first[w] = f"{cid} [P{pn}]"

    results: list[dict] = []
    # 自称
    for word, freq in self_counter.most_common(top_n):
        results.append({
            "word": word,
            "kind": "jishou" if src == "ja" else "自称",
            "freq": freq,
            "first": self_first.get(word, ""),
        })
    # 二人称
    for word, freq in second_counter.most_common(top_n):
        results.append({
            "word": word,
            "kind": "ninshou" if src == "ja" else "二人称",
            "freq": freq,
            "first": second_first.get(word, ""),
        })
    return results


def _gather_non_jis(
    paras: list[tuple[str, int, str]],
    house_style: dict | None,
) -> tuple[list[dict], list[dict]]:
    """非JIS字を集計。戻り値: (要決定リスト, 代用字表解決済みリスト)。"""
    all_text = "".join(t for _, _, t in paras)
    raw = find_non_jis_han(all_text)

    # house_style.toml の代用字表（substitution テーブル）を読む
    substitution: dict[str, str] = {}
    if house_style:
        rules = house_style.get("rules", [])
        for r in rules:
            if r.get("kind") == "kigou" and r.get("pattern") and r.get("replacement"):
                substitution[r["pattern"]] = r["replacement"]

    resolved: list[dict] = []
    undecided: list[dict] = []
    for ch, cnt in raw:
        entry = {"char": ch, "freq": cnt}
        if ch in substitution:
            entry["replacement"] = substitution[ch]
            resolved.append(entry)
        else:
            undecided.append(entry)
    return undecided, resolved


def _build_existing_map(work: Path) -> list[dict]:
    """characters.toml / terms.toml から既訳を集める。"""
    existing: list[dict] = []
    chars = load_characters(work)
    for c in chars.get("characters", []):
        src = (c.get("name_src") or "").strip()
        ja = (c.get("name_ja") or "").strip()
        if src and ja:
            existing.append({
                "src": src,
                "translation": ja,
                "source": "characters",
                "kind": "character",
            })
    terms = load_terms(work)
    for t in terms.get("terms", []):
        src = (t.get("term_src") or "").strip()
        ja = (t.get("term_ja") or "").strip()
        if src and ja:
            existing.append({
                "src": src,
                "translation": ja,
                "source": "terms",
                "kind": "term",
            })
    return existing


def _mark_existing(
    proper_nouns: list[dict],
    existing: list[dict],
) -> list[dict]:
    """proper_nouns の各語に対して既訳があるなら注釈を付ける。"""
    lookup: dict[str, str] = {}
    for e in existing:
        if e["src"] not in lookup:
            lookup[e["src"]] = e["translation"]
    out: list[dict] = []
    for pn in proper_nouns:
        w = pn["word"]
        if w in lookup:
            pn["existing"] = lookup[w]
        out.append(pn)
    return out


def _mark_existing_pronouns(
    pronouns: list[dict],
    existing: list[dict],
) -> list[dict]:
    """pronouns にも既訳を付ける（念のため）。"""
    lookup: dict[str, str] = {}
    for e in existing:
        if e["src"] not in lookup:
            lookup[e["src"]] = e["translation"]
    out: list[dict] = []
    for pr in pronouns:
        w = pr["word"]
        if w in lookup:
            pr["existing"] = lookup[w]
        out.append(pr)
    return out


# ── 命名方針（P0-5） ──────────────────────────────────────────────
# 固有名の扱い方を「音訳（カタカナ）」「漢語のまま」「それ以外」に分けて示す。
# 分類は読み・表記からの単純規則で、誤分類は許容（一覧は確認用）。
# 混在は誤りと判定せず「要確認」として提示するだけにする（作者が意図した
# 命名もありうる。当否は人間が決める）。

#: 固有名の扱い区分（表示順）
NAMING_CATEGORY_KATAKANA = "音訳（カタカナ）"
NAMING_CATEGORY_HAN = "漢語のまま"
NAMING_CATEGORY_OTHER = "それ以外"
NAMING_CATEGORIES = (
    NAMING_CATEGORY_KATAKANA,
    NAMING_CATEGORY_HAN,
    NAMING_CATEGORY_OTHER,
)

_KATAKANA_RE = re.compile(r"[\u30a1-\u30ff\u31f0-\u31ff]")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def _naming_category(form: str) -> str:
    """表記を扱い区分へ機械分類する（単純規則）。

    - カタカナが漢字より多い（カタカナ中心）→ 音訳（カタカナ）
    - 漢字がカタカナより多い（漢字表記）→ 漢語のまま
    - 同数・どちらも無い → それ以外
    誤分類は許容（一覧は確認用であり、混在を誤りとは判定しない）。
    """
    text = (form or "").strip()
    kata = len(_KATAKANA_RE.findall(text))
    han = len(_HAN_RE.findall(text))
    if kata > han:
        return NAMING_CATEGORY_KATAKANA
    if han > kata:
        return NAMING_CATEGORY_HAN
    return NAMING_CATEGORY_OTHER


def _load_naming_policy(work: Path) -> dict | None:
    """house_style.toml の [naming_policy] を共通探索先から読む（無ければ None）。

    探索先は #19 の house_style_paths（work/_house → work.parent/_house）。
    最初に見つかった、style を持つ方針を返す。
    """
    from vocab import house_style_paths

    for p in house_style_paths(work):
        try:
            data = load_toml(p)
        except Exception:
            continue
        pol = data.get("naming_policy")
        if isinstance(pol, dict) and str(pol.get("style") or "").strip():
            return pol
    return None


def _build_naming_list(proper_nouns: list[dict]) -> list[dict]:
    """固有名の扱い一覧を作る（扱いの判定に使う表記付き）。

    扱いの表記は、既訳（characters.toml / terms.toml）があればそれを、
    無ければ原語（原表記）を使う。
    """
    items: list[dict] = []
    for p in proper_nouns:
        word = str(p.get("word") or "").strip()
        if not word:
            continue
        form = str(p.get("existing") or "").strip() or word
        items.append({
            "word": word,
            "form": form,
            "category": _naming_category(form),
            "freq": p.get("freq", 0),
            "first": str(p.get("first") or ""),
        })
    return items


def _naming_groups(items: list[dict]) -> dict[str, list[dict]]:
    """扱い区分ごとの一覧（表示順: 音訳 → 漢語 → それ以外）。"""
    groups: dict[str, list[dict]] = {c: [] for c in NAMING_CATEGORIES}
    for it in items:
        groups.setdefault(it["category"], []).append(it)
    return groups


def _naming_is_mixed(items: list[dict]) -> bool:
    """複数の扱い区分が併存しているか（混在の判定。誤りとは判定しない）。"""
    groups = _naming_groups(items)
    return sum(1 for c in NAMING_CATEGORIES if groups.get(c)) >= 2


def _fmt_naming_md(items: list[dict], policy: dict | None) -> list[str]:
    """「固有名の扱い（命名方針）」節の Markdown 行を作る。"""
    groups = _naming_groups(items)
    mixed = _naming_is_mixed(items)
    lines: list[str] = ["## 固有名の扱い（命名方針）", ""]
    if policy:
        style = str(policy.get("style") or "").strip()
        scope = str(policy.get("scope") or "").strip()
        decided = str(policy.get("decided_at") or "").strip()
        note = str(policy.get("note") or "").strip()
        detail = ", ".join(x for x in (
            f"scope={scope}" if scope else "",
            f"decided_at={decided}" if decided else "",
        ) if x)
        line = f"- 命名方針: {style}" + (f"（{detail}）" if detail else "")
        if note:
            line += f" — note: {note}"
        lines.append(line)
    else:
        lines.append(
            "- 命名方針: 未決定（house_style.toml の [naming_policy] で決める。"
            "人名の裁定は両作品に同時適用される前提のため series スコープを推奨）"
        )
    if mixed and policy:
        lines.append(f"- 混在: あり（方針記録済み: {style}）")
    elif mixed:
        present = "と".join(c for c in NAMING_CATEGORIES if groups.get(c))
        lines.append(
            f"- 混在: 要確認（{present}が混在。作者が意図して付けた名前も"
            "ありうるため、確認の候補として提示します）"
        )
    else:
        lines.append("- 混在: なし")
    lines.append(
        "- 本文で名前が伏せられるキャラ（「女」「彼」等で呼ばれる）は、人物表で "
        "`core = true` を付けると compact コンテキストから落ちない（P1-4）"
    )
    lines.append("")
    for cat in NAMING_CATEGORIES:
        g = groups.get(cat, [])
        lines.append(f"### {cat}（{len(g)}件）")
        if g:
            lines.append("| 原語 | 表記 | 頻度 | 初出 |")
            lines.append("|---|---|---|---|")
            for it in g[:30]:
                lines.append(
                    f"| {it['word']} | {it['form']} | {it['freq']} | {it['first']} |"
                )
        else:
            lines.append("（該当なし）")
        lines.append("")
    return lines


# ── 出力フォーマット ──────────────────────────────────────────────


def _fmt_md(
    work_id: str,
    src: str,
    dst: str,
    para_count: int,
    proper_nouns: list[dict],
    pronouns: list[dict],
    undecided: list[dict],
    resolved: list[dict],
    existing: list[dict],
    naming_items: list[dict],
    naming_policy: dict | None,
) -> str:
    """lock_prep.md を生成。"""
    lines: list[str] = [
        f"# 初期訳語ロック: {work_id}",
        f"- 原語: {src} / 訳語言語: {dst} / 段落数: {para_count}",
        "",
    ]

    # 固有名詞（要決定のみ、上位30件）
    to_decide = [p for p in proper_nouns if "existing" not in p]
    lines.append("## 要決定（固有名詞候補）")
    if to_decide:
        lines.append("| 候補 | 頻度 | 初出 |")
        lines.append("|---|---|---|")
        for item in to_decide[:30]:
            lines.append(f"| {item['word']} | {item['freq']} | {item['first']} |")
    else:
        lines.append("（該当なし）")
    lines.append("")

    # 固有名の扱い（命名方針。P0-5）
    lines.extend(_fmt_naming_md(naming_items, naming_policy))

    # 自称・二人称
    lines.append("## 自称・二人称")
    if pronouns:
        lines.append("| 語 | 種別 | 頻度 | 初出 |")
        lines.append("|---|---|---|---|")
        for item in pronouns[:20]:
            suffix = f"（既訳: {item['existing']}）" if "existing" in item else ""
            lines.append(
                f"| {item['word']} | {item['kind']} | {item['freq']} | {item['first']} {suffix}|"
            )
    else:
        lines.append("（該当なし）")
    lines.append("")

    # 非JIS字
    lines.append(
        f"## 非JIS字（要決定: {len(undecided)}字 / 代用字表から解決: {len(resolved)}字）"
    )
    if undecided:
        lines.append("| 文字 | 頻度 |")
        lines.append("|---|---|")
        for item in undecided:
            lines.append(f"| {item['char']} | {item['freq']} |")
    if resolved:
        lines.append("")
        lines.append("### 代用字表から解決")
        lines.append("| 文字 | 頻度 | 置換後 |")
        lines.append("|---|---|---|")
        for item in resolved:
            lines.append(
                f"| {item['char']} | {item['freq']} | {item['replacement']} |"
            )
    lines.append("")

    # 既訳あり
    lines.append("## 既訳あり（characters.toml / terms.toml 参照）")
    if existing:
        lines.append("| 原語 | 既訳 | 出典 |")
        lines.append("|---|---|---|")
        for e in existing[:30]:
            lines.append(f"| {e['src']} | {e['translation']} | {e['source']}[[{e['kind']}]] |")
    else:
        lines.append("（該当なし）")
    lines.append("")

    return "\n".join(lines)


def _fmt_toml(
    proper_nouns: list[dict],
    pronouns: list[dict],
    undecided: list[dict],
    resolved: list[dict],
    existing: list[dict],
    naming_items: list[dict],
    naming_policy: dict | None,
) -> str:
    """lock_prep.toml を生成。"""
    data: dict = {
        "proper_nouns": proper_nouns,
        "pronouns": pronouns,
        "non_jis": undecided + [{"char": r["char"], "freq": r["freq"], "replacement": r["replacement"]} for r in resolved],
        "existing": existing,
        "naming": naming_items,
    }
    if naming_policy:
        data["naming_policy"] = dict(naming_policy)
    return dump_toml(data)


# ── CLI ────────────────────────────────────────────────────────────


def parse_args(argv: list[str] | None = None) -> Namespace:
    ap = ArgumentParser(description="初期訳語ロック：固有名詞・自称・非JIS字の機械集計")
    ap.add_argument("work", type=str, help="作品ディレクトリ (_saga/<ID>)")
    ap.add_argument("--output-md", type=str, default=None, help="出力 md パス")
    ap.add_argument("--output-toml", type=str, default=None, help="出力 toml パス")
    ap.add_argument("--top-n", type=int, default=50, help="固有名詞の上位件数 (default: 50)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    work = Path(args.work)
    if not work.is_dir():
        print(f"エラー: 作品ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1

    # 方向
    src, dst = direction(work)
    meta = load_chapters_meta(work)
    chapters = meta.get("chapters", [])
    para_count = sum(
        len(load_paras(work, c["id"]).get("paras", []))
        for c in chapters if c.get("id")
    )

    # 全段落読み込み
    all_paras = _load_all_paras(work, meta)

    # house_style.toml (任意。共通探索先)
    hs = _load_house_style(work)

    # 既存訳語
    existing = _build_existing_map(work)

    # 1. 固有名詞候補
    proper_nouns = _gather_proper_nouns(all_paras, src, args.top_n)
    proper_nouns = _mark_existing(proper_nouns, existing)

    # 2. 自称・二人称
    pronouns = _gather_pronouns(all_paras, src, args.top_n)
    pronouns = _mark_existing_pronouns(pronouns, existing)

    # 3. 非JIS字
    undecided, resolved = _gather_non_jis(all_paras, hs)

    # 4. 命名方針（P0-5）: 固有名の扱い一覧と [naming_policy] の状態
    naming_items = _build_naming_list(proper_nouns)
    naming_policy = _load_naming_policy(work)

    # 5. spicy プレースホルダ（骨格のみ）
    _spicy_placeholder = (
        "# TODO(P2-4): spicy 拡張があれば age_policy --check の出力をここに差し込む"
    )

    # ── 出力 ──────────────────────────────────────────────────
    work_id = work.name
    md_path = Path(args.output_md) if args.output_md else (work / "_work" / "lock_prep.md")
    toml_path = Path(args.output_toml) if args.output_toml else (work / "_work" / "lock_prep.toml")

    md_content = _fmt_md(
        work_id, src, dst, para_count,
        proper_nouns, pronouns, undecided, resolved, existing,
        naming_items, naming_policy,
    )
    toml_content = _fmt_toml(proper_nouns, pronouns, undecided, resolved, existing,
                             naming_items, naming_policy)

    md_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(md_path, md_content)
    atomic_write_text(toml_path, toml_content)

    # ── 標準出力（要約10行以内） ─────────────────────────────
    total_proper = len(proper_nouns)
    has_existing = sum(1 for p in proper_nouns if "existing" in p)
    to_decide = total_proper - has_existing

    print(f"[lock_prep] 作品ID={work_id}, 段落数={para_count}")
    print(f"  固有名詞候補: {total_proper}件（要決定: {to_decide}件、既訳あり: {has_existing}件）")
    print(f"  自称候補: {len(pronouns)}件 / 非JIS字: 要決定{len(undecided)}字、代用字表解決{len(resolved)}字")
    groups = _naming_groups(naming_items)
    mixed = _naming_is_mixed(naming_items)
    mixed_label = "要確認" if (mixed and not naming_policy) else ("あり" if mixed else "なし")
    print(
        f"  固有名の扱い: 音訳（カタカナ）{len(groups[NAMING_CATEGORY_KATAKANA])}件 / "
        f"漢語のまま{len(groups[NAMING_CATEGORY_HAN])}件 / "
        f"それ以外{len(groups[NAMING_CATEGORY_OTHER])}件（混在: {mixed_label}）"
    )
    if naming_policy:
        style = str(naming_policy.get("style") or "").strip()
        scope = str(naming_policy.get("scope") or "").strip()
        print(f"  命名方針: {style}" + (f"（scope={scope}）" if scope else ""))
    else:
        print("  命名方針: 未決定（house_style.toml の [naming_policy] で決める。series スコープ推奨）")
    print("  人物表: 本文で名前が伏せられるキャラは core = true を推奨（compact から落ちない。P1-4）")
    print(f"  出力: {md_path}, {toml_path}")
    print(f"  {_spicy_placeholder}")

    return 0


if __name__ == "__main__":
    sys.exit(main())