# -*- coding: utf-8 -*-
"""作品固有情報（作品名・キャラ名・作品ID）の再混入を防ぐガード（P1-3）。

背景: このリポジトリは実在の作品の翻訳作業から生まれたため、テスト・辞書・docs に
実在の作品名・キャラ名・作品ID・pixiv ID が混入しやすい。いちど除去した情報が
再び入るのを機械的に防ぐ。

検査1（作品IDパターン）:
    scripts/・references/・templates/・docs/（tracked）のテキストから、
    6〜12桁の単独数字列を検出する。合成ID（テスト定数）・ハッシュ断片
    （同じ数字の繰り返し）・このリポジトリの合成ID（888xxxx）以外は fail。

検査2（既知の固有名）:
    過去に除去した実トークン（作品名・キャラ名・作品ID）は平文で保持しない。
    sha256 の先頭12桁のリストだけを持ち、tracked の全テキストをトークン走査
    （トークン文字種の連続を切り出し、トークン長ごとの部分列をハッシュ照合）
    して再出現を検出する。置換はルート直下の md にも及ぶため、検査2は
    scripts/ 等に限らず tracked の全テキストを対象にする。

検出したら fail し、ファイル名と行を出す。除去手順は失敗メッセージに従い、
検出語を合成ラベル（例: 作品A / キャラX / 8880001）へ置換する（意味は保つ）。
既存の合成語（合成/テスト/張三 等）は触らない。
詳細: docs/phase12-taskV3-workinfo.md
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# 検査1の対象ディレクトリ（tracked のみを git ls-files で列挙する）。
SCAN_DIRS = ("scripts/", "references/", "templates/", "docs/")

# 6〜12桁の「単独」数字列。英数字とアンダースコアに隣接するものは識別子の
# 一部（例: 20260101_120000_ch001_b1 の日時成分）とみなし、単独扱いしない。
NUM_RE = re.compile(r"(?<![0-9A-Za-z_])([0-9]{6,12})(?![0-9A-Za-z_])")

# 許可リスト（合成ID: テストが作品IDとして使う定数）。
ALLOW_NUMBERS = frozenset({
    "10000000", "10000001",              # 画像名テストの合成ID
    "1010001", "1010002", "1110001",     # 連作検出テストの合成ID
    "1110002", "1110009", "1210001",
    "1210002", "2020001", "2020002",
    "2020003",
    "9990002",                           # series_detect の docstring 例（合成）
})

# 許可パターン:
# - 同じ数字の繰り返し（000000000000 / 11111111 等のハッシュ断片・プレースホルダ）
# - 888 + 3〜6桁（このリポジトリの合成ID。例: 8880001。部分IDテスト値 888000 を含む）
# バージョン数値は現状この形の該当なし。増えた場合はここへ理由つきで追加する。
ALLOW_NUM_PATTERNS = (
    re.compile(r"^(\d)\1+$"),
    re.compile(r"^888\d{3,6}$"),
)

REMOVAL_GUIDE = (
    "実在の作品名・キャラ名・作品ID・pixiv ID は合成ラベル（例: 作品A / キャラX / 8880001）へ\n"
    "置換して除去する（文脈の意味は保つ）。既存の合成語（合成/テスト/張三 等）は触らない。\n"
    "手順の詳細: docs/phase12-taskV3-workinfo.md"
)

# 検査2: 除去時に置換した実トークンの sha256 先頭12桁 → 文字数（走査窓の長さ）。
# トークン平文はここに置かない（名前そのものを repo に載せないため）。
KNOWN_TOKEN_HASHES = {
    # 人物名・作品名・フォルダ名
    "5c417fe17d81": 2, "945a4c565295": 4, "ddfcbd7ddbf8": 4,
    "a867fa96ecb7": 2, "83e117595d8c": 2, "01cdb04c9bfb": 2,
    "8b3af8cb1952": 2, "bb7b8051ecb2": 3, "0949a327724f": 3,
    "f45b0feef567": 2, "f379f5286152": 2, "f5db6a1d9069": 2,
    "35c90c9a01df": 2, "04c7758de970": 2, "612929871ef9": 2,
    "6f960c4e31e5": 3, "2c2924f8a5e3": 4, "a171ff1996b2": 4,
    "3a9c042e5c61": 5, "e2a41267271c": 3, "3c4ed1494536": 3,
    "575776edee29": 3, "e33ae8ac716d": 2, "6ddeab0e0a2f": 5,
    "5e34e65b02d8": 4,
    "71679db6a334": 15, "ccd4852a0a9f": 12, "d22258f924c1": 15,
    "e0a5fe0ca472": 20, "2b724dba9cdf": 5, "147642928b69": 13,
    # 作品ID・画像ID（数値。検査1と二重に守る）
    "c77fef6b8139": 9, "937b5ed9062b": 8, "578c91576de6": 8,
    "fef88bd309b5": 8, "98e4b8e1fd00": 8, "7f382f02efc4": 8,
    "14862411a1eb": 8, "adcd25078990": 7,
}

# トークン走査の切り出し: 英数字・アンダースコア・ハイフン・かな・漢字・
# 全角括弧・中黒の連続を1つの「トークン候補の連なり」として扱う。
TOKEN_RUN_RE = re.compile(
    r"[0-9A-Za-z_\-\u3040-\u30ff\u3400-\u9fff\uFF08\uFF09\u30FB]+"
)


def tracked_text_files() -> list[str]:
    """tracked のテキストファイル（git ls-files、UTF-8 で読めるものだけ）。"""
    try:
        out = subprocess.run(
            ["git", "ls-files"], cwd=str(REPO), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:  # git が使えない環境
        raise AssertionError(f"git ls-files を実行できない: {exc}")
    if out.returncode != 0:
        raise AssertionError(f"git ls-files 失敗: rc={out.returncode} {out.stderr}")
    rels = [r.strip() for r in out.stdout.splitlines() if r.strip()]
    ok: list[str] = []
    for rel in rels:
        try:
            (REPO / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        ok.append(rel)
    return ok


def find_number_violations(base: Path, rels: list[str]) -> list[str]:
    """検査1: 許可リスト外の6〜12桁単独数字列を "ファイル:行: 数字" で返す。"""
    hits: list[str] = []
    for rel in rels:
        try:
            text = (base / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            for m in NUM_RE.finditer(line):
                n = m.group(1)
                if n in ALLOW_NUMBERS:
                    continue
                if any(p.match(n) for p in ALLOW_NUM_PATTERNS):
                    continue
                hits.append(f"{rel}:{i}: {n}")
    return hits


def find_token_violations(
    base: Path, rels: list[str], hashes: dict[str, int] | None = None
) -> list[str]:
    """検査2: 既知トークンの再出現を "ファイル:行: sha256" で返す。

    同一ファイル内で同じ語は最初の出現だけ報告する（seen で重複を省く）。
    """
    table = KNOWN_TOKEN_HASHES if hashes is None else hashes
    lengths = sorted(set(table.values()))
    digests = {bytes.fromhex(h): (h, L) for h, L in table.items()}
    hits: list[str] = []
    for rel in rels:
        try:
            text = (base / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        seen: set[str] = set()
        for m in TOKEN_RUN_RE.finditer(text):
            run = m.group(0)
            n = len(run)
            for L in lengths:
                if L > n:
                    break
                for i in range(n - L + 1):
                    w = run[i:i + L]
                    if w in seen:
                        continue
                    seen.add(w)
                    d = hashlib.sha256(w.encode("utf-8")).digest()[:6]
                    hit = digests.get(d)
                    if hit is not None:
                        line = text.count("\n", 0, m.start() + i) + 1
                        hits.append(f"{rel}:{line}: 既知の固有名 sha256:{hit[0]}")
    return hits


class TestWorkIdPattern(unittest.TestCase):
    """検査1: 作品IDパターン（6〜12桁の単独数字列）。"""

    def test_scan_dirs_are_clean(self):
        """scripts/・references/・templates/・docs/ に許可外の数字列が無い。"""
        rels = [r for r in tracked_text_files() if r.startswith(SCAN_DIRS)]
        self.assertGreaterEqual(len(rels), 50, "tracked ファイルの取得に失敗した")
        hits = find_number_violations(REPO, rels)
        self.assertEqual(hits, [], "\n".join(hits) + "\n\n" + REMOVAL_GUIDE)

    def test_self_detects_non_synthetic_number(self):
        """自己テスト: 許可外の数字列を検出する（合成データ）。"""
        bad = f"{1234}{5678}"  # ソースに6桁以上の平文数字を置かないための組み立て
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.md"
            p.write_text(f"メモ: {bad} はIDらしい\n", encoding="utf-8")
            hits = find_number_violations(Path(td), ["sample.md"])
        self.assertEqual(len(hits), 1, hits)
        self.assertIn(bad, hits[0])
        self.assertIn("sample.md:1", hits[0])

    def test_self_allows_synthetic_and_fragments(self):
        """自己テスト: 合成ID・ハッシュ断片・識別子内の数字は通す（合成データ）。"""
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.md"
            p.write_text(
                "ID: 8880001 / 11111111 / 10000000 / 20260101_120000\n",
                encoding="utf-8",
            )
            hits = find_number_violations(Path(td), ["sample.md"])
        self.assertEqual(hits, [])


class TestKnownTokens(unittest.TestCase):
    """検査2: 置換済み実トークンの再出現（ハッシュ照合）。"""

    def test_tracked_files_are_clean(self):
        """tracked の全テキストに既知トークンが再出現していない。"""
        rels = tracked_text_files()
        self.assertGreaterEqual(len(rels), 100, "tracked ファイルの取得に失敗した")
        hits = find_token_violations(REPO, rels)
        self.assertEqual(hits, [], "\n".join(hits) + "\n\n" + REMOVAL_GUIDE)

    def test_self_detects_hashed_token(self):
        """自己テスト: ハッシュ照合でトークンを検出する（合成データ）。"""
        token = "合成テスト名"  # 実トークンではない（自己テスト用）
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.md"
            p.write_text(f"本文に {token} が出現する。\n", encoding="utf-8")
            hits = find_token_violations(Path(td), ["sample.md"], hashes={h: len(token)})
        self.assertEqual(len(hits), 1, hits)
        self.assertIn(h, hits[0])
        self.assertIn("sample.md:1", hits[0])

    def test_self_passes_without_token(self):
        """自己テスト: 該当が無ければ通す（合成データ）。"""
        token = "合成テスト名"
        h = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.md"
            p.write_text("本文に別の語が出現する。\n", encoding="utf-8")
            hits = find_token_violations(Path(td), ["sample.md"], hashes={h: len(token)})
        self.assertEqual(hits, [])

    def test_hash_list_holds_no_plaintext(self):
        """ハッシュリストの形式（12桁hex と文字数のみ。平文を持たない）。"""
        self.assertGreater(len(KNOWN_TOKEN_HASHES), 0)
        for h, length in KNOWN_TOKEN_HASHES.items():
            self.assertRegex(h, r"^[0-9a-f]{12}$")
            self.assertGreaterEqual(length, 1)


if __name__ == "__main__":
    unittest.main()
