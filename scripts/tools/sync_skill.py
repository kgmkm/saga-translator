"""スキル正本（リポジトリ）から実行環境の配布先へコピーし、差分を検査する。

スキルのコピーがリポジトリ・Hermes プロファイル・goose の .agents/skills に
分かれ、正本がどれか分からなくなる事故を防ぐための配布ツール。標準ライブラリのみ。

    python3 scripts/tools/sync_skill.py --dest <配布先スキルディレクトリ> [--check] [--source <リポジトリ>]

- --check はコピーせず差分一覧を出す。差分があれば exit 1、無ければ exit 0。
- 改行コード差（CRLF/LF）は差分として数えない。バイナリはバイト一致で判定。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# 配布するトップレベル項目。存在するものだけ扱う。
COPY_ITEMS = ("SKILL.md", "README.md", "references", "templates", "scripts")

# 配布しないディレクトリ名（作業成果物・開発者メモ・VCS・キャッシュ）。
EXCLUDE_DIRS = {
    ".git", "__pycache__", "docs",
    "_saga", "output", "translation_input", "originals", "chapters", "logs",
}

# 配布しない拡張子・ファイル名のパターン。
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".tmp", ".orig", ".rej", ".swp")
EXCLUDE_NAMES = {"Thumbs.db", ".DS_Store"}


def _is_excluded(rel: Path) -> bool:
    """スキルルートからの相対パスが配布対象外か。"""
    for part in rel.parts:
        if part in EXCLUDE_DIRS:
            return True
    if rel.suffix in EXCLUDE_SUFFIXES:
        return True
    if rel.name in EXCLUDE_NAMES:
        return True
    return False


def _is_text(p: Path) -> bool:
    """テキストファイルらしいか（改行正規化比較の対象）。"""
    try:
        data = p.read_bytes()
    except OSError:
        return False
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def _normalized_lines(p: Path) -> list[str]:
    """改行コードを正規化した行リスト。"""
    text = p.read_bytes().decode("utf-8", errors="surrogateescape")
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def files_equal(a: Path, b: Path) -> bool:
    """内容が等しいか。テキストは改行コード差を無視、バイナリはバイト一致。"""
    if not b.exists():
        return False
    if _is_text(a) and _is_text(b):
        return _normalized_lines(a) == _normalized_lines(b)
    # filecmp.cmp は stat 署名キーの _cache を持ち、mtime 粒度の粗い環境で
    # 別ファイルを同一と誤判定する（順序依存フレークの実例あり）。キャッシュを使わない。
    if a.stat().st_size != b.stat().st_size:
        return False
    return a.read_bytes() == b.read_bytes()


def iter_source_files(source: Path):
    """配布対象ファイルを (相対パス, 絶対パス) で列挙する。"""
    for item in COPY_ITEMS:
        top = source / item
        if not top.exists():
            continue
        if top.is_file():
            if not _is_excluded(Path(item)):
                yield Path(item), top
            continue
        for p in sorted(top.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(source)
            if _is_excluded(rel):
                continue
            yield rel, p


def sync(source: Path, dest: Path, check: bool) -> int:
    """配布を実行（または検査）する。戻り値: 終了コード（--check 時のみ非0があり得る）。"""
    if not source.is_dir():
        print(f"[sync] エラー: 配布元が見つかりません: {source}", file=sys.stderr)
        return 2
    if not (source / "SKILL.md").exists():
        print(f"[sync] エラー: SKILL.md が無いのでスキルルートではありません: {source}",
              file=sys.stderr)
        return 2

    copied = 0
    updated = 0
    identical = 0
    differ: list[str] = []
    planned_new: list[str] = []
    planned_upd: list[str] = []

    for rel, sp in iter_source_files(source):
        dp = dest / rel
        if not dp.exists():
            if check:
                differ.append(str(rel).replace("\\", "/"))
                planned_new.append(str(rel).replace("\\", "/"))
            else:
                dp.parent.mkdir(parents=True, exist_ok=True)
                dp.write_bytes(sp.read_bytes())
                copied += 1
            continue
        if files_equal(sp, dp):
            identical += 1
        else:
            differ.append(str(rel).replace("\\", "/"))
            if check:
                planned_upd.append(str(rel).replace("\\", "/"))
            else:
                dp.parent.mkdir(parents=True, exist_ok=True)
                dp.write_bytes(sp.read_bytes())
                updated += 1

    # 配布先にしか無いファイル（正本から消えた残骸）も差分として報告
    stale: list[str] = []
    if dest.is_dir():
        source_rels = {str(rel).replace("\\", "/") for rel, _ in iter_source_files(source)}
        for dp in dest.rglob("*"):
            if not dp.is_file():
                continue
            rel = dp.relative_to(dest)
            if _is_excluded(rel):
                continue
            rel_s = str(rel).replace("\\", "/")
            if rel.parts and rel.parts[0] in COPY_ITEMS and rel_s not in source_rels:
                stale.append(rel_s)

    mode = "check" if check else "sync"
    print(f"[sync] mode={mode} source={source} dest={dest}")
    if check:
        print(f"[sync] identical={identical} differ={len(differ)}")
        for name in differ:
            kind = "new" if name in planned_new else "changed"
            print(f"[sync]   differ({kind}): {name}")
        for name in sorted(stale):
            print(f"[sync]   stale(配布先にのみ): {name}")
        if differ or stale:
            return 1
        return 0

    print(f"[sync] copied={copied} updated={updated} identical={identical}")
    for name in sorted(stale):
        print(f"[sync]   stale(配布先にのみ・未削除): {name}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="スキル正本を配布先へ同期する")
    ap.add_argument("--dest", required=True, help="配布先スキルディレクトリ")
    ap.add_argument("--source", default=None,
                    help="リポジトリのスキルルート（既定: このスクリプトの2つ上）")
    ap.add_argument("--check", action="store_true",
                    help="コピーせず差分のみ検査（差分があれば exit 1）")
    args = ap.parse_args()

    source = Path(args.source).resolve() if args.source else Path(__file__).resolve().parent.parent.parent
    dest = Path(args.dest).resolve()
    raise SystemExit(sync(source, dest, args.check))


if __name__ == "__main__":
    main()