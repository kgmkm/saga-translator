"""zh→ja 方向のターゲット側（日本語）チェック。qa_translation.py から使う。

_lib.find_non_jis_han と同じ基準（CP932 外＋言語 pack の NON_JIS_HAN）。
日本語訳に残った簡体字・原文の誤字・未登録の固有名詞を検出する。

拡張: P0-3 で Finding / ResidueConfig / find_zh_vocab / find_cn_residue_toml /
find_term_src_leftover を追加。
"""
from __future__ import annotations

import pathlib
import re
import sys
import tomllib
from dataclasses import dataclass, field

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from _lib import find_non_jis_han  # noqa: E402

__all__ = [
    "Finding",
    "ResidueConfig",
    "find_non_jis_han",
    "find_zh_vocab",
    "find_cn_residue_toml",
    "find_cn_residue_fields",
    "find_term_src_leftover",
]

# ── 内部データ ──────────────────────────────────────────────────────────

_DEF_DATA: pathlib.Path = (pathlib.Path(__file__).resolve().parent /
                           "data" / "zh2ja_residue.toml")


def _load_def_toml() -> dict[str, dict]:
    """組み込み辞書 TOML を読む。無ければ空 dict。"""
    try:
        with open(_DEF_DATA, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def _load_vocab(cfg: ResidueConfig | None) -> dict[str, str]:
    d = _load_def_toml()
    base: dict[str, str] = {}
    base.update(d.get("vocab", {}))
    if cfg is not None:
        base.update(cfg.vocab)
    return base


def _load_idioms(cfg: ResidueConfig | None) -> dict[str, str]:
    d = _load_def_toml()
    base: dict[str, str] = {}
    base.update(d.get("idioms", {}))
    if cfg is not None:
        base.update(cfg.idioms)
    return base


def _load_cn_words() -> list[str]:
    d = _load_def_toml()
    return list(d.get("cn_words", {}))


def _load_allow(cfg: ResidueConfig | None) -> set[str]:
    d = _load_def_toml()
    base: set[str] = set(d.get("allow", {}))
    if cfg is not None:
        base |= cfg.allow
    return base


def _load_simp() -> dict[str, str]:
    d = _load_def_toml()
    return dict(d.get("simp", {}))


# ── データ構造 ──────────────────────────────────────────────────────────

@dataclass
class Finding:
    """1件の検出結果。rule は検出種別、value は検出された語、context は
    前後20字、line は行番号（無ければ0）、safe_fix は文脈非依存で安全な
    置換先（無ければ None）。"""
    rule: str
    value: str
    context: str
    line: int = 0
    safe_fix: str | None = None


@dataclass
class ResidueConfig:
    """検出設定。known_terms: 意図的に簡体字を含む既定訳（除外）。
    allow: 誤検知許容ワード。vocab/idioms: 追加の中國語→日本語対応表。"""
    known_terms: set[str] = field(default_factory=set)
    allow: set[str] = field(default_factory=set)
    vocab: dict[str, str] = field(default_factory=dict)
    idioms: dict[str, str] = field(default_factory=dict)


# ── 検出関数 ────────────────────────────────────────────────────────────

def _context(s: str, pos: int, width: int = 20) -> str:
    """文字列 s の位置 pos（検出語の先頭）の前後 width 字を返す。"""
    start = max(0, pos - width)
    end = min(len(s), pos + width)
    ctx = s[start:end]
    if start > 0:
        ctx = "…" + ctx
    if end < len(s):
        ctx = ctx + "…"
    return ctx


def find_zh_vocab(text: str, config: ResidueConfig | None = None) -> list[Finding]:
    """中国語語彙・成語（字は日本語正字でも語が中国語）を検出する。"""
    findings: list[Finding] = []
    vocab = _load_vocab(config)
    idioms = _load_idioms(config)
    simp = _load_simp()
    allow = _load_allow(config)
    known = set(config.known_terms) if config is not None else set()
    jp_ok = set("沉着震荡捨無張見開現据結社対応戦涙間問門魚鳥馬車東書様誰話説発尽図団羅蘭銅鐵")
    src_re = re.compile(r"(?:name_src|term_src|target_src|title_src)\s*=")

    lines = text.split("\n")
    for lineno, line in enumerate(lines, 1):
        if src_re.search(line):
            continue
        if line.lstrip().startswith(">"):  # 対訳原文ブロック
            continue

        probe = line
        # known_terms を除去
        for t in known:
            probe = probe.replace(t, "")

        allow_clean = set()
        for a in allow:
            allow_clean.add(a)
        for a in allow_clean:
            if a in probe:
                probe = probe.replace(a, "")

        # VOCAB
        for kw, rep in vocab.items():
            idx = probe.find(kw)
            if idx >= 0:
                findings.append(Finding(
                    rule="vocab",
                    value=kw,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

        # IDIOMS
        for kw, rep in idioms.items():
            idx = probe.find(kw)
            if idx >= 0:
                findings.append(Finding(
                    rule="idiom",
                    value=kw,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

        # SIMP — 簡体字専用文字
        for ch, rep in simp.items():
            idx = probe.find(ch)
            if idx >= 0 and ch not in jp_ok:
                findings.append(Finding(
                    rule="simp",
                    value=ch,
                    context=_context(probe, idx),
                    line=lineno,
                    safe_fix=rep,
                ))

    return findings


def find_cn_residue_toml(toml_path: pathlib.Path,
                         config: ResidueConfig | None = None) -> list[Finding]:
    """分析TOMLの日本語フィールドに残った中国語を検出する。

    *_src キーの値・コメント行・age_policy_report は除外。
    検出規則 R1 (CP932外CJK), R2 (機能語), R3 (定型語),
    R4 (全角読点), R5 (人称/呼称)。
    """
    findings: list[Finding] = []
    toml_path = pathlib.Path(toml_path)

    if "age_policy_report" in toml_path.name:
        return findings

    cn_words = _load_cn_words()
    cn_mark = set("你我她她们这吗呢哦呀嘛怎什为从对给还让说您")
    allow = _load_allow(config)

    keyval_re = re.compile(
        r'\b(?:name_src|term_src|target_src|title_src|src)\s*=\s*"(?:[^"\\]|\\.)*"'
    )
    person_re = re.compile(r'(?:first_person|second_person)\s*=\s*"[^"]*[你我您]')
    form_re = re.compile(r'form\s*=\s*"[^"]*[你我您]')

    try:
        text = toml_path.read_text(encoding="utf-8").replace("\r\n", "\n")
    except OSError:
        return findings

    for lineno, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue

        probe = keyval_re.sub("", line)

        # allow 除去
        for a in allow:
            probe = probe.replace(a, "")

        reasons: list[str] = []

        # R1: CP932 外の CJK
        for c in probe:
            if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf":
                try:
                    c.encode("cp932")
                except UnicodeEncodeError:
                    reasons.append(f"非JIS字:{c}")

        # R3: 定型語
        for w in cn_words:
            if w in probe:
                reasons.append(f"中文語:{w}")

        # R4: 全角読点
        if "，" in probe:
            reasons.append("中文読点")

        # R2: 機能語
        cnt = sum(1 for c in probe if c in cn_mark)
        has_kana = bool(re.search(r"[ぁ-んァ-ヴー]", probe))
        if cnt >= 2 and not has_kana:
            reasons.append(f"中文機能語x{cnt}")

        # R5: 人称/呼称
        if person_re.search(probe) or form_re.search(probe):
            reasons.append("人称/呼称に中文")

        if reasons:
            findings.append(Finding(
                rule="cn_field",
                value=" / ".join(reasons),
                context=line.strip()[:80],
                line=lineno,
            ))

    return findings


def find_cn_residue_fields(data, config: ResidueConfig | None = None) -> list[Finding]:
    """パース済み TOML データの「対象言語の欄」に残った中国語を検出する。

    キーが ``src`` / ``*_src`` の値は原文側として除外し、dict / list を再帰して
    文字列値だけを検査する（行の正規表現では ``*_src`` の全形を除外できない）。
    検出規則は find_cn_residue_toml と同じ:

    - R1: CP932 外の CJK（非JIS字）
    - R2: 中国語の機能語（かな無しで2字以上）
    - R3: 定型語（cn_words。既定は空）
    - R4: 全角読点（，）
    - R5: 人称/呼称の欄（first_person / second_person / form）に中国語の人称

    空のデータ（記録なし・コメントのみの TOML）は検出なし（「未分析」扱いと矛盾しない）。
    """
    findings: list[Finding] = []
    cn_words = _load_cn_words()
    cn_mark = set("你我她她们这吗呢哦呀嘛怎什为从对给还让说您")
    allow = _load_allow(config)
    kana_re = re.compile(r"[ぁ-んァ-ヴー]")

    def _is_src_key(key: str) -> bool:
        # aliases は別名・愛称・原文の異表記を保持する欄であり、原語形を含む
        # ことが仕様（P0-4）。原文側として残渣検査から除外する。
        return key == "src" or key.endswith("_src") or key == "aliases"

    def _check(key: str, value: str, path: str) -> None:
        probe = value
        for a in allow:
            probe = probe.replace(a, "")

        reasons: list[str] = []

        # R1: CP932 外の CJK
        for c in probe:
            if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf":
                try:
                    c.encode("cp932")
                except UnicodeEncodeError:
                    reasons.append(f"非JIS字:{c}")

        # R3: 定型語
        for w in cn_words:
            if w in probe:
                reasons.append(f"中文語:{w}")

        # R4: 全角読点
        if "，" in probe:
            reasons.append("中文読点")

        # R2: 機能語
        cnt = sum(1 for c in probe if c in cn_mark)
        if cnt >= 2 and not kana_re.search(probe):
            reasons.append(f"中文機能語x{cnt}")

        # R5: 人称/呼称
        if key in ("first_person", "second_person", "form") and any(
                c in probe for c in "你我您"):
            reasons.append("人称/呼称に中文")

        if reasons:
            findings.append(Finding(
                rule="cn_field",
                value=" / ".join(reasons),
                context=f"{path} = {value[:60]}",
                line=0,
            ))

    def _walk(obj, path: str) -> None:
        if isinstance(obj, dict):
            for k, v in obj.items():
                key = str(k)
                if _is_src_key(key):
                    continue
                child = f"{path}.{key}" if path else key
                if isinstance(v, str):
                    _check(key, v, child)
                else:
                    _walk(v, child)
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                _walk(v, f"{path}[{i}]")
        elif isinstance(obj, str) and path:
            _check("", obj, path)

    _walk(data, "")
    return findings


def find_term_src_leftover(work: pathlib.Path) -> list[Finding]:
    """訳文に terms.toml の term_src が残っていないか検出する。

    terms.toml の term_src 値を収集し、訳文ファイル内にそのまま残っていないか
    スキャンする。
    """
    findings: list[Finding] = []
    work = pathlib.Path(work)

    terms_path = work / "terms.toml"
    if not terms_path.is_file():
        return findings

    # term_src を収集
    try:
        with open(terms_path, "rb") as f:
            terms_data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return findings

    term_src_list: list[str] = []
    entries = terms_data
    if isinstance(entries, dict):
        for row in entries.get("terms", []):
            if isinstance(row, dict):
                src = row.get("term_src", "")
                if src:
                    term_src_list.append(src)

    if not term_src_list:
        return findings

    # 訳文ファイルをスキャン（output 下の .md / .txt）
    output_dirs = list(work.glob("output/*/"))
    for out_dir in output_dirs:
        ch_dirs = [out_dir]
        ch_dirs.extend(sorted(out_dir.glob("ch*")))
        for d in ch_dirs:
            for fpath in sorted(d.glob("*.md")) + sorted(d.glob("*.txt")):
                try:
                    content = fpath.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line_num, line in enumerate(content.split("\n"), 1):
                    for src in term_src_list:
                        if src in line:
                            findings.append(Finding(
                                rule="term_src",
                                value=src,
                                context=line.strip()[:80],
                                line=line_num,
                            ))

    return findings


# ── CLI ─────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="zh→ja 残渣検出: 日本語訳に残った中国語語彙・簡体字を検出")
    parser.add_argument("path", nargs="+", help="ファイルまたはディレクトリ")
    parser.add_argument("--json", action="store_true",
                        help="JSON 形式で機械可読出力")
    args = parser.parse_args()

    all_findings: list[dict] = []

    for p in args.path:
        p_obj = pathlib.Path(p)
        if p_obj.is_dir():
            for f in sorted(p_obj.rglob("*")):
                if f.suffix in (".toml", ".md", ".txt"):
                    all_findings.extend(_scan_file(f))
        elif p_obj.is_file():
            all_findings.extend(_scan_file(p_obj))

    seen = len(all_findings)

    if args.json:
        import json
        print(json.dumps(all_findings, ensure_ascii=False, indent=2))
    else:
        if seen == 0:
            print("✅ 検出なし")
        else:
            print(f"🔍 {seen} 件検出")
            for fd in all_findings[:10]:
                ctx = fd["context"][:60]
                sf = fd.get("safe_fix") or ""
                print(f"  L{fd['line']} [{fd['rule']}] "
                      f"{fd['value']}{' → ' + sf if sf else ''}: {ctx}")
            if seen > 10:
                print(f"  … 他 {seen - 10} 件（--json で全件表示）")

    return 1 if seen else 0


def _scan_file(fpath: pathlib.Path) -> list[dict]:
    fpath = pathlib.Path(fpath)
    if fpath.suffix == ".toml":
        findings = find_cn_residue_toml(fpath)
    else:
        findings = find_zh_vocab(fpath.read_text(encoding="utf-8"))
    name = fpath.relative_to(pathlib.Path.cwd()) if fpath.is_relative_to(
        pathlib.Path.cwd()) else fpath
    return [{
        "file": str(name),
        "rule": f.rule,
        "value": f.value,
        "context": f.context,
        "line": f.line,
        "safe_fix": f.safe_fix,
    } for f in findings]


if __name__ == "__main__":
    sys.exit(_main())
