"""ingest の版記録と frontmatter ヘッダ、prepare のドリフト警告のテスト。

すべて合成データ・一時ディレクトリで完結し、実プロファイルには触れない。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402

PYTHON = sys.executable or "python3"


def _run(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [PYTHON, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def _write(p: Path, text: str, newline: str = "\n") -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline=newline)


# 合成の frontmatter 作品（実際の作品情報は使わない）。
_FM_SRC = (
    "---\n"
    "title: 测试合成标题\n"
    "author: 合成作者\n"
    "tags: [test, synth]\n"
    "---\n\n"
    "第一章 开始\n\n"
    "天空阴沉沉的。她站在山门前。\n\n"
    "低沉的钟声从远处传来，他握紧了手中的剑。\n\n"
    "第二章 相遇\n\n"
    "第二天清晨，她出现在客栈门口。\n"
)

# ダウンロード形式ヘッダ（既存挙動の回帰用）。
_DL_SRC = (
    "合成題名\n\n"
    "合成著者\n\n"
    "https://example.com/n/1\n\n"
    "2024-01-01\n\n"
    "#合成\n\n"
    "--------以下正文--------\n\n"
    "第一章 开始\n\n"
    "天空阴沉沉的。她站在山门前。\n\n"
    "低沉的钟声从远处传来，他握紧了手中的剑。\n"
)


class TestIngestManifestVersion(unittest.TestCase):
    """ingest が manifest に skill_version / skill_commit を記録する。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _ingest(self, filename: str, text: str) -> tuple[subprocess.CompletedProcess, Path]:
        src = self.td / filename
        _write(src, text)
        out_parent = self.td / "out"
        p = _run(str(SCRIPTS / "ingest.py"), str(src),
                 "-o", str(out_parent), "--dst", "ja")
        works = [d for d in out_parent.iterdir() if d.is_dir()] if out_parent.exists() else []
        return p, (works[0] if works else out_parent)

    def test_manifest_records_skill_version_and_commit(self):
        p, work = self._ingest("synth.txt", _DL_SRC)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        man = _lib.load_structured(work / "manifest")
        # version は SKILL.md frontmatter の値。
        self.assertEqual(man.get("skill_version"), _lib.read_skill_version(
            Path(__file__).resolve().parent.parent.parent))
        # commit は git 非依存で必ず文字列（"unknown" でも可）。
        self.assertIsInstance(man.get("skill_commit"), str)
        self.assertTrue(man.get("skill_commit"))

    def test_other_manifest_keys_preserved(self):
        p, work = self._ingest("synth.txt", _DL_SRC)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        man = _lib.load_structured(work / "manifest")
        for key in ("lang", "dst", "title", "n_paras", "work_dir"):
            self.assertIn(key, man)


class TestIngestFrontmatterHeader(unittest.TestCase):
    """frontmatter ヘッダ入力の回帰（旧 fname NameError の再発防止）。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _ingest(self, filename: str, text: str) -> tuple[subprocess.CompletedProcess, Path]:
        src = self.td / filename
        _write(src, text)
        out_parent = self.td / "out"
        p = _run(str(SCRIPTS / "ingest.py"), str(src),
                 "-o", str(out_parent), "--dst", "ja")
        works = [d for d in out_parent.iterdir() if d.is_dir()] if out_parent.exists() else []
        return p, (works[0] if works else out_parent)

    def test_frontmatter_header_exit0_and_title(self):
        p, work = self._ingest("synth_fm.txt", _FM_SRC)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        man = _lib.load_structured(work / "manifest")
        self.assertEqual(man.get("title"), "测试合成标题")
        self.assertEqual(man.get("author"), "合成作者")

    def test_frontmatter_header_files_written(self):
        p, work = self._ingest("synth_fm.txt", _FM_SRC)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        self.assertTrue((work / "manifest.toml").exists())
        self.assertTrue((work / "originals" / "source.md").exists())

    def test_frontmatter_without_title_falls_back_to_filename(self):
        """frontmatter に title が無い場合、ファイル名の題にフォールバックする。

        旧実装はここで `fname` 未定義の NameError を起こしていた（回帰防止）。
        """
        src_text = ("---\nauthor: 合成作者\n---\n\n"
                    "第一章 开始\n\n天空阴沉沉的。她站在山门前。\n\n"
                    "低沉的钟声从远处传来。\n")
        p, work = self._ingest("合成題名-フォールバック.txt", src_text)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        man = _lib.load_structured(work / "manifest")
        self.assertEqual(man.get("title"), "合成題名-フォールバック")

    def test_download_header_still_works(self):
        """ダウンロード形式（--------以下正文--------）の既存挙動を壊さない。"""
        src = self.td / "synth_dl.txt"
        _write(src, _DL_SRC)
        out_parent = self.td / "out"
        p = _run(str(SCRIPTS / "ingest.py"), str(src),
                 "-o", str(out_parent), "--dst", "ja")
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        work = next(d for d in out_parent.iterdir() if d.is_dir())
        man = _lib.load_structured(work / "manifest")
        self.assertEqual(man.get("title"), "合成題名")
        self.assertEqual(man.get("author"), "合成著者")


class TestParseFrontmatter(unittest.TestCase):
    """_lib.parse_frontmatter の単体テスト。"""

    def test_parses_scalars_and_list(self):
        meta, body = _lib.parse_frontmatter("---\ntitle: t\nauthor: a\ntags: [x, y]\n---\nbody")
        self.assertEqual(meta["title"], "t")
        self.assertEqual(meta["author"], "a")
        self.assertEqual(meta["tags"], ["x", "y"])
        self.assertEqual(body, "body")

    def test_none_when_no_frontmatter(self):
        meta, body = _lib.parse_frontmatter("本文だけ。")
        self.assertIsNone(meta)
        self.assertEqual(body, "本文だけ。")

    def test_none_when_unclosed(self):
        meta, body = _lib.parse_frontmatter("---\ntitle: t\n本文")
        self.assertIsNone(meta)

    def test_crlf_tolerated(self):
        meta, body = _lib.parse_frontmatter("---\r\ntitle: t\r\n---\r\nbody")
        self.assertEqual(meta["title"], "t")
        self.assertEqual(body, "body")


class TestPrepareDriftWarning(unittest.TestCase):
    """saga.py prepare のスキル版ドリフト警告。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self):
        import shutil
        shutil.rmtree(self.td, ignore_errors=True)

    def _make_work(self, skill_version: str | None) -> Path:
        work = self.td / "work"
        mv = f'skill_version = "{skill_version}"\n' if skill_version is not None else ""
        _write(work / "manifest.toml", f'lang = "zh"\ndst = "ja"\n{mv}')
        _write(work / "chapters_meta.toml",
               '[[chapters]]\nid = "ch001"\nblocks = [{id="b1", paras=[1,2]}]\n')
        _write(work / "chapters" / "ch001.paras.toml",
               '[[paras]]\nn = 1\ntext = "第一段。测试。"\n\n'
               '[[paras]]\nn = 2\ntext = "第二段。她笑了。"\n')
        return work

    def _prepare(self, work: Path) -> subprocess.CompletedProcess:
        return _run(str(SCRIPTS / "saga.py"), "prepare", str(work))

    def test_drift_emits_stderr_warning_and_continues(self):
        # 実行中の版と確実に異なる架空の版を記録する。
        work = self._make_work("0.0.0-notreal")
        p = self._prepare(work)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        self.assertIn("警告", p.stderr)
        self.assertIn("0.0.0-notreal", p.stderr)

    def test_matching_version_no_warning(self):
        running = _lib.read_skill_version(Path(__file__).resolve().parent.parent.parent)
        if running is None:
            self.skipTest("SKILL.md に version が無い")
        work = self._make_work(running)
        p = self._prepare(work)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        self.assertNotIn("スキル版が", p.stderr)

    def test_no_recorded_version_no_warning(self):
        work = self._make_work(None)
        p = self._prepare(work)
        self.assertEqual(p.returncode, 0, msg=p.stderr)
        self.assertNotIn("スキル版が", p.stderr)


if __name__ == "__main__":
    unittest.main()