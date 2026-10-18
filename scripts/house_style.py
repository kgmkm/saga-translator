#!python3
"""house_style.py — ハウススタイル管理 CLI

Subcommands:
  build <saga_root>      分析 TOML から候補を列挙（stdout, status: candidate）
  validate <toml_path>   スキーマ適合検査（行番号付き報告、終了コード 1）
  get <toml_path>        フィルタ抽出（--scope, --kind, --status, --target）
"""
from __future__ import annotations

import re
import sys
import tomllib
from argparse import ArgumentParser, Namespace
from datetime import datetime, timezone
from pathlib import Path

# ── 定数 ──────────────────────────────────────────────────────────────

VALID_KINDS = frozenset({
    "naming", "ruby", "keishou", "jishou", "ninshou",
    "kigou", "kugirisen", "goi", "sonota",
})
VALID_SCOPES = frozenset({"work", "series", "character", "global"})
VALID_STATUSES = frozenset({"candidate", "approved", "locked"})
REQUIRED_RULE_KEYS = frozenset({"id", "kind", "scope", "pattern", "status", "version"})
STR_KEYS = frozenset({"id", "kind", "scope", "target", "pattern", "replacement", "preference", "rationale", "status"})
INT_KEYS = frozenset({"version"})

# ── ユーティリティ ────────────────────────────────────────────────────

def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _esc_toml(v: str) -> str:
    """TOML 文字列リテラルにエスケープ。"""
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def _dump_rules(rules: list[dict]) -> str:
    """ルールリストを [[rules]] 形式の TOML 文字列に。"""
    out: list[str] = []
    for r in rules:
        out.append("[[rules]]")
        for k in ("id", "kind", "scope", "target", "pattern", "replacement",
                   "preference", "rationale", "status", "version", "evidence"):
            v = r.get(k)
            if v is None:
                continue
            if k == "evidence":
                items = ", ".join(_esc_toml(x) for x in v)
                out.append(f"evidence = [{items}]")
            elif k == "version":
                out.append(f"version = {v}")
            else:
                out.append(f"{k} = {_esc_toml(str(v))}")
        out.append("")
    return "\n".join(out)


def _read_analysis_toml(path: Path) -> list[dict]:
    """TOML から配列 of テーブルのエントリを読む。無ければ空リスト。"""
    if not path.exists():
        return []
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError:
        # CRLF などの問題で落ちる場合の fallback
        try:
            data = tomllib.loads(raw.decode("utf-8").replace("\r\n", "\n"))
        except tomllib.TOMLDecodeError:
            return []
    # どのキー名でも動くよう先頭の配列を返す
    for v in data.values():
        if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
            return v
    return []


# ── build ──────────────────────────────────────────────────────────────

def cmd_build(args: Namespace) -> int:
    """分析 TOML から候補を列挙。stdout に TOML 形式で出力。"""
    saga_root = Path(args.saga_root)
    if not saga_root.is_dir():
        print(f"エラー: saga_root が見つかりません: {saga_root}", file=sys.stderr)
        return 1

    # 配下の全作品ディレクトリを収集
    work_dirs: list[Path] = []
    for entry in sorted(saga_root.iterdir()):
        if entry.is_dir() and re.match(r"^\d+$", entry.name):
            analysis_dir = entry / "analysis"
            if analysis_dir.is_dir():
                work_dirs.append(entry)

    if not work_dirs:
        print(f"エラー: saga_root 配下に作品ディレクトリが見つかりません: {saga_root}", file=sys.stderr)
        return 1

    rules: list[dict] = []
    seen_patterns: dict[str, list[dict]] = {}  # pattern → [rule, ...]

    for wd in work_dirs:
        work_id = wd.name
        analysis_dir = wd / "analysis"

        # characters
        for fname in analysis_dir.glob("characters*.toml"):
            chars = _read_analysis_toml(fname)
            for ch in chars:
                name_src = (ch.get("name_src") or "").strip()
                name_ja = (ch.get("name_ja") or "").strip()
                ruby = (ch.get("ruby") or "").strip()
                fp = (ch.get("first_person") or "").strip()
                sp = (ch.get("second_person") or "").strip()

                # 自称 → candidate (jishou)
                if fp:
                    rule = {
                        "id": f"fp_{name_src}_{work_id}",
                        "kind": "jishou",
                        "scope": "character",
                        "target": name_src,
                        "pattern": name_src,
                        "preference": fp,
                        "rationale": f"{work_id} characters から抽出",
                        "status": "candidate",
                        "version": 1,
                        "evidence": [f"{work_id}/characters"],
                    }
                    rules.append(rule)
                    seen_patterns.setdefault(name_src, []).append(rule)

                # 二人称 → candidate (ninshou)
                if sp:
                    rule = {
                        "id": f"sp_{name_src}_{work_id}",
                        "kind": "ninshou",
                        "scope": "character",
                        "target": name_src,
                        "pattern": name_src,
                        "preference": sp,
                        "rationale": f"{work_id} characters から抽出",
                        "status": "candidate",
                        "version": 1,
                        "evidence": [f"{work_id}/characters"],
                    }
                    rules.append(rule)
                    seen_patterns.setdefault(name_src, []).append(rule)

                # ルビ（ruby 欄がある場合）→ candidate
                if name_ja and ruby:
                    rule = {
                        "id": f"ruby_{name_src}_{work_id}",
                        "kind": "ruby",
                        "scope": "character",
                        "target": name_src,
                        "pattern": name_ja,
                        "replacement": f"{{{name_ja}|{ruby}}}",
                        "rationale": f"{work_id} characters から抽出",
                        "status": "candidate",
                        "version": 1,
                        "evidence": [f"{work_id}/characters"],
                    }
                    rules.append(rule)
                    seen_patterns.setdefault(name_ja, []).append(rule)

        # terms
        terms_path = analysis_dir / "terms.toml"
        terms = _read_analysis_toml(terms_path)
        for t in terms:
            term_src = (t.get("term_src") or "").strip()
            term_ja = (t.get("term_ja") or "").strip()
            ruby = (t.get("ruby") or "").strip()
            if term_src and term_ja:
                rule = {
                    "id": f"goi_{term_src}_{work_id}",
                    "kind": "goi",
                    "scope": "work",
                    "target": work_id,
                    "pattern": term_src,
                    "replacement": term_ja,
                    "rationale": f"{work_id} terms から抽出",
                    "status": "candidate",
                    "version": 1,
                    "evidence": [f"{work_id}/terms"],
                }
                rules.append(rule)
                if ruby:
                    rubyr = {
                        "id": f"ruby_goi_{term_src}_{work_id}",
                        "kind": "ruby",
                        "scope": "work",
                        "target": work_id,
                        "pattern": term_ja,
                        "replacement": f"{{{term_ja}|{ruby}}}",
                        "rationale": f"{work_id} terms ルビから抽出",
                        "status": "candidate",
                        "version": 1,
                        "evidence": [f"{work_id}/terms"],
                    }
                    rules.append(rubyr)

    # 競合警告（同じ pattern に複数の replacement がある場合）
    conflicts = 0
    for pat, rs in seen_patterns.items():
        replacements = {r.get("replacement") or r.get("preference") for r in rs if r.get("replacement") or r.get("preference")}
        if len(replacements) > 1:
            print(f"# 競合: pattern={pat} → {replacements}", file=sys.stderr)
            conflicts += 1

    # TOML 出力
    header = (
        f"# house_style candidates (auto-generated by build)\n"
        f"# Generated: {_now_utc()}\n"
        f"# Conflicts reported on stderr: {conflicts}\n"
    )
    body = _dump_rules(rules)
    output = header + f"version = 1\ncreated_at = {_esc_toml(_now_utc())}\nupdated_at = {_esc_toml(_now_utc())}\n\n" + body

    output_path = args.output
    if output_path:
        Path(output_path).write_text(output, encoding="utf-8", newline="\n")
        print(f"候補を書き出しました: {output_path}", file=sys.stderr)
    else:
        sys.stdout.write(output)
        sys.stdout.flush()

    return 0


# ── validate ──────────────────────────────────────────────────────────

def cmd_validate(args: Namespace) -> int:
    """スキーマ適合を検査し、違反を行番号付きで報告。"""
    path = _resolve_style_path(Path(args.toml_path))
    if not path.is_file():
        print(f"エラー: house_style.toml が見つかりません: {path}", file=sys.stderr)
        print("ヒント: `python3 scripts/house_style.py init <work>` で作成できます。",
              file=sys.stderr)
        return 1

    lines = path.read_text(encoding="utf-8").splitlines()
    try:
        data = tomllib.loads("\n".join(lines))
    except tomllib.TOMLDecodeError as e:
        print(f"TOML構文エラー: {e}", file=sys.stderr)
        return 1

    # トップレベル必須
    errors: list[str] = []
    for key in ("version", "created_at", "updated_at"):
        if key not in data:
            errors.append(f"行1: トップレベルの必須キー '{key}' がありません")
    if "rules" not in data or not isinstance(data.get("rules"), list):
        errors.append("行1: 必須キー 'rules'（配列）がありません")
        print("\n".join(errors), file=sys.stderr)
        return 1

    rules: list[dict] = data["rules"]
    seen_ids: set[str] = set()

    for i, rule in enumerate(rules, 1):
        # 必須キー
        for key in REQUIRED_RULE_KEYS:
            if key not in rule:
                errors.append(f"rules[{i}]: 必須キー '{key}' がありません")
        # 型チェック
        for key in STR_KEYS:
            v = rule.get(key)
            if v is not None and not isinstance(v, str):
                errors.append(f"rules[{i}]: '{key}' は文字列であるべきです (実際: {type(v).__name__})")
        # version は整数
        vv = rule.get("version")
        if vv is not None and not isinstance(vv, int):
            errors.append(f"rules[{i}]: 'version' は整数であるべきです (実際: {type(vv).__name__})")
        # id 重複
        rid = rule.get("id")
        if rid and isinstance(rid, str):
            if rid in seen_ids:
                errors.append(f"rules[{i}]: id '{rid}' が重複しています")
            seen_ids.add(rid)
        # kind バリデーション
        kind = rule.get("kind")
        if kind and kind not in VALID_KINDS:
            errors.append(f"rules[{i}]: 未知の kind '{kind}'（許容: {', '.join(sorted(VALID_KINDS))}）")
        # scope バリデーション
        scope = rule.get("scope")
        if scope and scope not in VALID_SCOPES:
            errors.append(f"rules[{i}]: 未知の scope '{scope}'（許容: {', '.join(sorted(VALID_SCOPES))}）")
        # status バリデーション
        status = rule.get("status")
        if status and status not in VALID_STATUSES:
            errors.append(f"rules[{i}]: 未知の status '{status}'（許容: {', '.join(sorted(VALID_STATUSES))}）")
        # scope が global 以外で target 必須
        if scope and scope != "global" and "target" not in rule:
            errors.append(f"rules[{i}]: scope='{scope}' の場合 target が必須です"
                          "（作品スコープなら作品 ID。例: target = \"<work_id>\"）")
        # evidence は配列
        ev = rule.get("evidence")
        if ev is not None and not isinstance(ev, list):
            errors.append(f"rules[{i}]: 'evidence' は配列であるべきです (実際: {type(ev).__name__})")
        # replacement と preference の二者択一（両方あるのは警告）
        has_rep = "replacement" in rule
        has_pref = "preference" in rule
        if has_rep and has_pref:
            errors.append(f"rules[{i}]: 'replacement' と 'preference' の両方は指定できません（二者択一）")

    # [naming_policy]（任意。G14 / T3）: codename はコードネームの扱いで
    # katakana / sinitic / keep。style は人名用の既存キー。
    pol = data.get("naming_policy")
    if pol is not None:
        if not isinstance(pol, dict):
            errors.append("[naming_policy]: テーブルであるべきです")
        else:
            for key, allowed in (("style", ("transliterate", "sinitic", "mixed")),
                                 ("scope", ("work", "series")),
                                 ("codename", ("katakana", "sinitic", "keep"))):
                v = pol.get(key)
                if v is None:
                    continue
                if not isinstance(v, str):
                    errors.append(f"[naming_policy]: '{key}' は文字列であるべきです (実際: {type(v).__name__})")
                elif v not in allowed:
                    errors.append(f"[naming_policy]: 未知の {key} '{v}'（許容: {', '.join(allowed)}）")
            for key in ("note", "decided_at"):
                v = pol.get(key)
                if v is not None and not isinstance(v, str):
                    errors.append(f"[naming_policy]: '{key}' は文字列であるべきです (実際: {type(v).__name__})")

    if errors:
        for e in errors:
            print(e, file=sys.stderr)
        return 1

    print(f"OK: {len(rules)} ルール、すべてスキーマ適合。", file=sys.stderr)
    return 0


# ── get ────────────────────────────────────────────────────────────────

def _resolve_style_path(path: Path) -> Path:
    """ファイルでも作品ディレクトリでも house_style.toml のパスを返す。

    作品ディレクトリを渡された場合は `<work>/_house/house_style.toml` を優先し、
    無ければ直下の `<work>/house_style.toml` を返す（後者は読まれない誤配置だが、
    validate で中身を検査できるようにする）。
    """
    if path.is_dir():
        for c in (path / "_house" / "house_style.toml", path / "house_style.toml"):
            if c.is_file():
                return c
        return path / "_house" / "house_style.toml"
    return path


def cmd_init(args: Namespace) -> int:
    """`<work>/_house/house_style.toml` を作る（直下の誤配置があれば中身を移す）。"""
    work = Path(args.work).resolve()
    if not work.is_dir():
        print(f"エラー: 作品ディレクトリが見つかりません: {work}", file=sys.stderr)
        return 1
    dest = work / "_house" / "house_style.toml"
    if dest.exists():
        print(f"[house_style] 既にあります: {dest}", file=sys.stderr)
        return 1
    root = work / "house_style.toml"
    if root.is_file():
        text = root.read_text(encoding="utf-8")
        src_label = f"{root}（直下の誤配置。内容を引き継ぎました）"
    else:
        tpl = Path(__file__).resolve().parent.parent / "templates" / "house_style.toml"
        text = tpl.read_text(encoding="utf-8")
        src_label = "テンプレート"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    print(f"[house_style] 作成: {dest}")
    print(f"[house_style] 元: {src_label}")
    try:
        has_rules = "rules" in tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        print(f"[house_style] 作成しましたが、TOML として読めません: {e}", file=sys.stderr)
        return 0
    if not has_rules:
        print("[house_style] ルールは未記入です（雛形のみ）。"
              "[[rules]] を足してから validate してください。")
        return 0
    if cmd_validate(Namespace(toml_path=str(dest))) != 0:
        print("[house_style] 作成は完了しましたが、スキーマ違反があります"
              "（house_style.py validate で確認してください）", file=sys.stderr)
        if root.is_file():
            print(f'[house_style] ヒント: scope="work" のルールには '
                  f'target = "{work.name}" を足してください', file=sys.stderr)
    return 0


def cmd_get(args: Namespace) -> int:
    """フィルタ抽出。マッチしたルールを TOML 形式で stdout に。"""
    path = _resolve_style_path(Path(args.toml_path))
    if not path.is_file():
        print(f"エラー: house_style.toml が見つかりません: {path}", file=sys.stderr)
        print("ヒント: `python3 scripts/house_style.py init <work>` で作成できます。",
              file=sys.stderr)
        return 1

    data = tomllib.loads(path.read_bytes().decode("utf-8"))
    rules: list[dict] = data.get("rules", [])

    # フィルタ
    result = []
    for r in rules:
        if args.scope and r.get("scope") != args.scope:
            continue
        if args.kind and r.get("kind") != args.kind:
            continue
        if args.status and r.get("status") != args.status:
            continue
        if args.target and r.get("target") != args.target:
            continue
        result.append(r)

    # TOML 出力（ヘッダは出力しない — フィルタ結果だけ）
    sys.stdout.write(_dump_rules(result))
    sys.stdout.flush()
    return 0


# ── CLI ────────────────────────────────────────────────────────────────

def build_parser() -> ArgumentParser:
    p = ArgumentParser(description="house_style.toml 管理 CLI")
    sub = p.add_subparsers(dest="command", required=True)

    # build
    bp = sub.add_parser("build", help="分析 TOML から候補を列挙")
    bp.add_argument("saga_root", help="saga_root ディレクトリ（例: _saga/）")
    bp.add_argument("--output", "-o", help="出力先ファイル（省略時は stdout）")

    # init
    ip = sub.add_parser("init", help="<work>/_house/house_style.toml を作る")
    ip.add_argument("work", help="作品ディレクトリ（manifest のある場所）")

    # validate
    vp = sub.add_parser("validate", help="スキーマ適合を検査")
    vp.add_argument("toml_path", help="house_style.toml へのパス")

    # get
    gp = sub.add_parser("get", help="フィルタ抽出")
    gp.add_argument("toml_path", help="house_style.toml へのパス")
    gp.add_argument("--scope", "-s", choices=sorted(VALID_SCOPES), help="scope で絞り込み")
    gp.add_argument("--kind", "-k", choices=sorted(VALID_KINDS), help="kind で絞り込み")
    gp.add_argument("--status", "-t", choices=sorted(VALID_STATUSES), help="status で絞り込み")
    gp.add_argument("--target", help="target で絞り込み")

    return p


def main() -> None:
    p = build_parser()
    args = p.parse_args()
    if args.command == "build":
        sys.exit(cmd_build(args))
    elif args.command == "init":
        sys.exit(cmd_init(args))
    elif args.command == "validate":
        sys.exit(cmd_validate(args))
    elif args.command == "get":
        sys.exit(cmd_get(args))


if __name__ == "__main__":
    main()