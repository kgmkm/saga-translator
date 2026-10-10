"""phase7-taskP1: 辞書共通 API（vocab.py）と prepare / commit 接続のテスト。

合成データのみ使用（実在の語彙表エントリ・作品情報は使わない）。
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import vocab  # noqa: E402
# テストの環境独立: 兄弟ディレクトリの拡張 vocab.toml（自動探索）に依存しない
os.environ.setdefault("SAGA_VOCAB_SKIP_AUTO", "1")


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@contextlib.contextmanager
def _vocab_env(*paths: Path):
    """SAGA_VOCAB_PATH を一時的に設定する（終了時に元へ戻す）。"""
    old = os.environ.get("SAGA_VOCAB_PATH")
    os.environ["SAGA_VOCAB_PATH"] = os.pathsep.join(str(p) for p in paths)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("SAGA_VOCAB_PATH", None)
        else:
            os.environ["SAGA_VOCAB_PATH"] = old


def _make_work(base: Path, name: str = "test_work") -> Path:
    """最小の翻訳可能な作品ディレクトリ（zh→ja）。合成語のみ。"""
    work = base / name
    _write(work / "manifest.toml", 'lang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml", """\
[[chapters]]
id = "ch001"
blocks = [{id="b1", paras=[1,3]}]
""")
    _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "合成語B闪着光。"

[[paras]]
n = 2
text = "合成語C发光。"

[[paras]]
n = 3
text = "合成語D伸长。"
""")
    return work


def _make_draft(work: Path, paras: list[tuple[int, str]]) -> Path:
    draft = work / "output" / "_work" / "ch001_b1_ja_numbered.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    draft.write_text("\n\n".join(f"[P{n:03d}] {t}" for n, t in paras),
                     encoding="utf-8", newline="\n")
    return draft


# 合成の拡張 vocab.toml（version "1"）。幽鏡 は原文に出ない語（差し込み対象外）。
_VOCAB_TOML = """\
version = "1"

[[vocab]]
src = "合成語B"
dst = "合成訳B"
kind = "fixed"

[[vocab]]
src = "合成語C"
src_variants = ["魅镜"]
dst = "合成訳C"
kind = "candidates"

[[vocab]]
src = "合成語D"
dst = "合成語D"
kind = "ja_ok"

[[vocab]]
src = "幽鏡"
dst = "蒼鏡"
kind = "candidates"
"""


def _ext_dir(td: Path, toml: str = _VOCAB_TOML, name: str = "ext") -> Path:
    d = td / name
    _write(d / "vocab.toml", toml)
    return d


def _house_style(rules: list[dict], residue: dict | None = None) -> str:
    parts = ["version = 1", ""]
    for r in rules:
        parts.append("[[rules]]")
        for k in ("id", "kind", "scope", "target", "pattern", "replacement", "status"):
            if k in r:
                parts.append(f'{k} = "{r[k]}"')
        parts.append("version = 1")
        parts.append("")
    if residue is not None:
        parts.append("[residue]")
        items = ", ".join(f'"{x}"' for x in residue.get("known_terms", []))
        parts.append(f"known_terms = [{items}]")
    return "\n".join(parts) + "\n"


class _TempBase(unittest.TestCase):
    def setUp(self) -> None:
        self.td = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        import shutil
        os.environ.pop("SAGA_VOCAB_PATH", None)
        shutil.rmtree(self.td, ignore_errors=True)


# ── 優先順位 ────────────────────────────────────────────────────────────


class TestPriority(_TempBase):
    """作品 terms ＞ house_style ＞ 拡張。同じ src は上位が勝つ。"""

    def test_terms_beat_house_style_beat_ext(self) -> None:
        work = _make_work(self.td)
        ext = _ext_dir(self.td)
        house = work / "_house" / "house_style.toml"
        terms = work / "analysis" / "terms.toml"
        with _vocab_env(ext):
            _write(terms, '[[terms]]\nterm_src = "合成語B"\nterm_ja = "合成語B玉"\n')
            _write(house, _house_style([
                {"id": "g1", "kind": "goi", "scope": "global",
                 "pattern": "合成語B", "replacement": "合成語B石", "status": "approved"},
            ]))
            v = vocab.load_vocab(work)
            e = v.lookup("合成語B")
            self.assertEqual(e.dst, "合成語B玉")  # 作品 terms が最上位
            self.assertEqual(e.origin, "terms")

            terms.unlink()
            v = vocab.load_vocab(work)
            e = v.lookup("合成語B")
            self.assertEqual(e.dst, "合成語B石")  # 次に house_style
            self.assertEqual(e.origin, "house_style")

            house.unlink()
            v = vocab.load_vocab(work)
            e = v.lookup("合成語B")
            self.assertEqual(e.dst, "合成訳B")  # 最後に拡張
            self.assertEqual(e.origin, "vocab")

    def test_house_style_scope_filter(self) -> None:
        """scope=global / series / work(target一致) を採用し、他作品・character は採らない。"""
        work = _make_work(self.td)
        _write(work / "_house" / "house_style.toml", _house_style([
            {"id": "g1", "kind": "goi", "scope": "global",
             "pattern": "合成語B", "replacement": "合成語B石", "status": "approved"},
            {"id": "w1", "kind": "goi", "scope": "work", "target": work.name,
             "pattern": "合成語C", "replacement": "合成訳C", "status": "locked"},
            {"id": "w2", "kind": "goi", "scope": "work", "target": "別作品",
             "pattern": "幽鏡", "replacement": "蒼鏡", "status": "approved"},
            {"id": "c1", "kind": "goi", "scope": "character", "target": "誰か",
             "pattern": "合成語D", "replacement": "合成語D", "status": "approved"},
            {"id": "g2", "kind": "goi", "scope": "global",
             "pattern": "消えた語", "replacement": "消えた訳", "status": "candidate"},
            # series は連作共有ルールとして適用する（#43。従来は黙って無視されていた）
            {"id": "s1", "kind": "goi", "scope": "series", "target": "シリーズA",
             "pattern": "連作語", "replacement": "連作訳", "status": "approved"},
        ]))
        v = vocab.load_vocab(work)
        self.assertEqual(v.lookup("合成語B").dst, "合成語B石")
        self.assertEqual(v.lookup("合成語C").dst, "合成訳C")
        self.assertEqual(v.lookup("連作語").dst, "連作訳")   # series は採用
        self.assertIsNone(v.lookup("幽鏡"))    # 別作品の scope=work は対象外
        self.assertIsNone(v.lookup("合成語D"))    # character は対象外（既存の表示と同じ）
        self.assertIsNone(v.lookup("消えた語"))  # candidate は未承認


# ── 原文字形索引 ────────────────────────────────────────────────────────


class TestOriginalGlyphIndex(_TempBase):
    """src / src_variants の両方の字形で引ける（別字形の経路）。"""

    def test_lookup_by_both_forms(self) -> None:
        work = _make_work(self.td)
        ext = _ext_dir(self.td)
        with _vocab_env(ext):
            v = vocab.load_vocab(work)
            by_variant = v.lookup("魅镜")   # 別字形（見出しは 合成語C）
            by_src = v.lookup("合成語C")
            self.assertIsNotNone(by_variant)
            self.assertEqual(by_variant.src, "合成語C")
            self.assertIs(by_variant, by_src)
            self.assertIsNone(v.lookup("存在しない語"))

    def test_select_by_source_matches_variant(self) -> None:
        """原文が別字形でも prepare の選別（原語一致）に載る。"""
        work = _make_work(self.td)
        ext = _ext_dir(self.td)
        with _vocab_env(ext):
            v = vocab.load_vocab(work)
            selected = v.select_by_source(["ここに魅镜が出る。"])
            self.assertEqual([e.src for e in selected], ["合成語C"])


# ── 辞書版 ──────────────────────────────────────────────────────────────


class TestDictVersion(_TempBase):
    def test_version_returned(self) -> None:
        work = _make_work(self.td)
        ext = _ext_dir(self.td, _VOCAB_TOML.replace('version = "1"', 'version = "3"'))
        with _vocab_env(ext):
            self.assertEqual(vocab.load_vocab(work).dict_version(), "3")

    def test_default_none(self) -> None:
        work = _make_work(self.td)
        self.assertEqual(vocab.load_vocab(work).dict_version(), "none")

    def test_multiple_files_joined(self) -> None:
        work = _make_work(self.td)
        ext1 = _ext_dir(self.td, 'version = "1"\n', name="ext1")
        ext2 = _ext_dir(self.td, 'version = "2"\n', name="ext2")
        with _vocab_env(ext1, ext2):
            self.assertEqual(vocab.load_vocab(work).dict_version(), "1+2")


# ── house_style の2探索先（#19） ────────────────────────────────────────


class TestHouseStylePaths(_TempBase):
    """prepare と commit が同じ2探索先（work と work 親）を使う。"""

    def _make_nested_work(self) -> Path:
        # 実運用と同じ形: <base>/_saga/<ID>。親は _saga。
        return _make_work(self.td / "saga" / "_saga", "001")

    def test_both_paths_and_merge(self) -> None:
        work = self._make_nested_work()
        w_house = work / "_house" / "house_style.toml"
        p_house = work.parent / "_house" / "house_style.toml"
        _write(w_house, _house_style([
            {"id": "r_work", "kind": "goi", "scope": "global",
             "pattern": "幽鏡", "replacement": "蒼鏡", "status": "approved"},
        ]))
        _write(p_house, _house_style([
            {"id": "r_parent", "kind": "goi", "scope": "work", "target": work.name,
             "pattern": "合成語C", "replacement": "合成訳C", "status": "approved"},
            # 同じ src は work 側が勝つ
            {"id": "r_parent2", "kind": "goi", "scope": "global",
             "pattern": "幽鏡", "replacement": "別鏡", "status": "approved"},
        ]))
        self.assertEqual(vocab.house_style_paths(work), [w_house, p_house])

        v = vocab.load_vocab(work)
        self.assertEqual(v.lookup("幽鏡").dst, "蒼鏡")  # work 側が優先
        self.assertEqual(v.lookup("合成語C").dst, "合成訳C")  # 親側も読める

        # prepare 側（build_context）も同じ関数で2探索先を読む
        from build_context import _load_house_style_rules
        ids = [r.get("id") for r in _load_house_style_rules(work)]
        self.assertIn("r_work", ids)
        self.assertIn("r_parent", ids)

    def test_parent_only_and_none(self) -> None:
        work = self._make_nested_work()
        self.assertEqual(vocab.house_style_paths(work), [])
        p_house = work.parent / "_house" / "house_style.toml"
        _write(p_house, _house_style([]))
        self.assertEqual(vocab.house_style_paths(work), [p_house])

    def test_commit_detects_parent_rule(self) -> None:
        """commit 側も親の house_style を読む（残渣で停止）。"""
        work = self._make_nested_work()
        _write(work.parent / "_house" / "house_style.toml", _house_style([
            {"id": "r_parent", "kind": "goi", "scope": "global",
             "pattern": "幽鏡", "replacement": "蒼鏡", "status": "approved"},
        ]))
        _make_draft(work, [(1, "静かな光。"), (2, "普通の訳文。"), (3, "幽鏡が光る。")])
        from saga import cmd_commit
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cmd_commit(work, "ch001", "b1", "test-model", "v1")
        self.assertEqual(rc, 1)
        text = out.getvalue() + err.getvalue()
        self.assertIn("幽鏡", text)
        self.assertIn("蒼鏡", text)
        self.assertFalse((work / "output" / "ch001" / "b1_ja.md").exists())


# ── prepare への差し込み ────────────────────────────────────────────────


class TestPrepareInsertion(_TempBase):
    def _context(self, work: Path) -> str:
        from build_context import _stage_impl
        _stage_impl(work, "ch001", "b1", "translation")
        return (work / "translation_input" / "ch001_b1_translation.md").read_text(
            encoding="utf-8")

    def _section(self, ctx: str) -> str:
        self.assertIn("## 訳語(辞書。原文に出現した語はこれに従う)", ctx)
        return ctx.split("## 訳語(辞書", 1)[1].split("\n## ", 1)[0]

    def test_source_words_inserted(self) -> None:
        """原文に出現する語（dst・kind・src）だけを訳語セクションに差し込む。"""
        work = _make_work(self.td)
        ext = _ext_dir(self.td)
        with _vocab_env(ext):
            sec = self._section(self._context(work))
        self.assertIn("合成語B → 合成訳B [fixed]", sec)
        self.assertIn("合成語C（魅镜） → 合成訳C [candidates]", sec)
        self.assertIn("合成語D → 合成語D [ja_ok]", sec)
        self.assertNotIn("幽鏡", sec)  # 原文に出ない語は差し込まない

    def test_variant_in_source_is_selected(self) -> None:
        """原文が別字形（魅镜）でも選別される。"""
        work = _make_work(self.td)
        _write(work / "chapters" / "ch001.paras.toml", """\
[[paras]]
n = 1
text = "合成語B闪着光。"

[[paras]]
n = 2
text = "魅镜发光。"

[[paras]]
n = 3
text = "合成語D伸长。"
""")
        ext = _ext_dir(self.td)
        with _vocab_env(ext):
            sec = self._section(self._context(work))
        self.assertIn("合成語C（魅镜） → 合成訳C [candidates]", sec)

    def test_terms_are_not_duplicated(self) -> None:
        """作品 terms は既存の用語集が扱う（訳語セクションには重複して出さない）。"""
        work = _make_work(self.td)
        _write(work / "analysis" / "terms.toml",
               '[[terms]]\nterm_src = "合成語C"\nterm_ja = "合成訳C"\n')
        ext = _ext_dir(self.td)
        with _vocab_env(ext):
            ctx = self._context(work)
        self.assertIn("合成語C", ctx)          # 用語集には出る
        sec = self._section(ctx)
        self.assertNotIn("合成語C", sec)       # 訳語セクションには出さない

    def test_no_vocab_no_section(self) -> None:
        """差し込む語が無ければ何もしない（従来どおり）。"""
        work = _make_work(self.td)
        ctx = self._context(work)
        self.assertNotIn("## 訳語(辞書", ctx)


# ── commit の残渣検査 ───────────────────────────────────────────────────


class TestCommitVocab(_TempBase):
    def setUp(self) -> None:
        super().setUp()
        self.ext = _ext_dir(self.td)
        self.work = _make_work(self.td)

    def _commit(self, work: Path | None = None) -> int:
        from saga import cmd_commit
        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            return cmd_commit(work or self.work, "ch001", "b1", "test-model", "v1")

    def _commit_capture(self, work: Path | None = None) -> tuple[int, str]:
        from saga import cmd_commit
        with contextlib.redirect_stdout(io.StringIO()) as out, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = cmd_commit(work or self.work, "ch001", "b1", "test-model", "v1")
        return rc, out.getvalue() + err.getvalue()

    def _emitted(self) -> Path:
        return self.work / "output" / "ch001" / "b1_ja.md"

    def _draft_hash(self) -> str:
        draft_p = self.work / "output" / "_work" / "ch001_b1_ja_numbered.md"
        return hashlib.sha256(draft_p.read_bytes()).hexdigest()[:12]

    def _add_ruling(self, paragraph: int, draft_hash: str) -> None:
        from rulings import add_ruling
        from saga import _analysis_src_hash
        from _lib import load_chapters_meta
        meta = load_chapters_meta(self.work)
        add_ruling(
            self.work, work_id=self.work.name, chapter="ch001", block="b1",
            paragraph=paragraph, rule_id="vocab:合成語C",
            src_hash=_analysis_src_hash(self.work, "ch001/b1", meta),
            draft_hash=draft_hash,
            dict_version=vocab.load_vocab(self.work).dict_version(),
            rationale="合成テストの裁定", adjudicator="test",
            disposition="allow", status="active")

    def test_fixed_residue_stops(self) -> None:
        """fixed: 原語形が訳文に残る → commit 停止（語と規定訳を出す）。"""
        _make_draft(self.work, [(1, "合成語Bが光る。"), (2, "合成訳Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        self.assertIn("残渣(fixed)", text)
        self.assertIn("合成語B", text)
        self.assertIn("合成訳B", text)
        self.assertFalse(self._emitted().exists())

    def test_fixed_missing_dst_stops(self) -> None:
        """fixed: 原文に語が出ているのに規定訳が訳文に無い → commit 停止。"""
        _make_draft(self.work, [(1, "光る石がある。"), (2, "合成訳Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        self.assertIn("残渣(fixed)", text)
        self.assertIn("合成訳B", text)
        self.assertFalse(self._emitted().exists())

    def test_fixed_with_dst_passes(self) -> None:
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成訳Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc = self._commit()
        self.assertEqual(rc, 0)
        self.assertTrue(self._emitted().exists())

    def test_candidates_residue_stops(self) -> None:
        """candidates: 原語相当の語が残る → commit 停止。"""
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成語Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc, text = self._commit_capture()
        self.assertEqual(rc, 1)
        self.assertIn("残渣(candidates)", text)
        self.assertIn("合成語C", text)
        self.assertIn("合成訳C", text)
        self.assertFalse(self._emitted().exists())
        # report.json にも辞書の検査結果が残る（#73: stops は検出語の明細配列）
        snap_dirs = sorted((self.work / "output" / "_work" / "commit").iterdir())
        import json
        report = json.loads((snap_dirs[-1] / "report.json").read_text(encoding="utf-8"))
        stops = report["stages"]["residue"]["vocab"]["stops"]
        self.assertIsInstance(stops, list)
        self.assertEqual([s["value"] for s in stops], ["合成語C"])
        self.assertEqual(stops[0]["kind"], "candidates")

    def test_ja_ok_excluded(self) -> None:
        """ja_ok: 日本語としても正当な語は検査から除外。"""
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成訳Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc = self._commit()
        self.assertEqual(rc, 0)
        self.assertTrue(self._emitted().exists())

    def test_candidates_ruling_passes(self) -> None:
        """candidates は全キー一致の裁定記録（rule_id=vocab:<src>）で通過する。"""
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成語Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc1 = self._commit()
            self.assertEqual(rc1, 1)
            self._add_ruling(paragraph=2, draft_hash=self._draft_hash())
            rc2 = self._commit()
        self.assertEqual(rc2, 0)
        self.assertTrue(self._emitted().exists())

    def test_candidates_ruling_other_draft_hash_invalid(self) -> None:
        """裁定記録は全キー一致のみ有効（draft_hash が違えば通らない）。"""
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成語Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            self.assertEqual(self._commit(), 1)
            self._add_ruling(paragraph=2, draft_hash="000000000000")
            rc = self._commit()
        self.assertEqual(rc, 1)
        self.assertFalse(self._emitted().exists())

    def test_known_terms_waives_candidates(self) -> None:
        """known_terms（作品全体で残す語）に登録すると検査から外れる。"""
        _write(self.work / "_house" / "house_style.toml",
               _house_style([], residue={"known_terms": ["合成語C"]}))
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成語Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc = self._commit()
        self.assertEqual(rc, 0)

    def test_timeline_reason_stays_residue(self) -> None:
        """辞書だけが止めた場合も timeline の失敗理由は residue のまま。"""
        import json
        _make_draft(self.work, [(1, "合成訳Bが光る。"), (2, "合成語Cを拭く。"), (3, "合成語Dが伸びる。")])
        with _vocab_env(self.ext):
            rc = self._commit()
        self.assertEqual(rc, 1)
        lines = (self.work / "logs" / "timeline.jsonl").read_text(
            encoding="utf-8").splitlines()
        commits = [json.loads(l) for l in lines if l.strip()]
        last = [e for e in commits if e.get("stage") == "commit"][-1]
        self.assertEqual(last["reason"], "residue")


class TestAliasAfterPriorityOverride(unittest.TestCase):
    """優先順位で src が上書きされても、下位の別字形（src_variants）の索引を失わない。

    作品 terms が拡張 vocab と同じ src を持つ実運用（terms に転記される）では、
    上書き前のエントリが持つ別字形エイリアスが消えると原文字形索引が機能しなくなる。
    """

    def test_variant_alias_survives_override(self):
        import os
        import tempfile
        from unittest import mock
        from pathlib import Path
        import vocab as vocab_mod

        td = Path(tempfile.mkdtemp(prefix="vocab_alias_"))
        work = td / "testwork"
        (work / "analysis").mkdir(parents=True)
        # 作品 terms（上位）が拡張の src を上書き
        (work / "analysis" / "terms.toml").write_text(
            'terms = [{ term_src = "合成語A", term_ja = "作品規定訳" }]\n', encoding="utf-8")
        ext = td / "ext" / "references"
        ext.mkdir(parents=True)
        (ext / "vocab.toml").write_text(
            'version = "7"\n\n[[vocab]]\nsrc = "合成語A"\nsrc_variants = ["合成語A"]\n'
            'dst = "拡張訳"\nkind = "fixed"\n', encoding="utf-8")
        with mock.patch.dict(os.environ, {"SAGA_VOCAB_PATH": str(ext / "vocab.toml")}):
            v = vocab_mod.load_vocab(work)
        hit = v.lookup("合成語A")
        self.assertIsNotNone(hit)
        self.assertEqual(hit.dst, "作品規定訳")      # 上位が勝つ
        alias = v.lookup("合成語A")                      # 別字形の索引は残る
        self.assertIsNotNone(alias, "別字形の索引が上書きで消えた")
        self.assertEqual(alias.src, "合成語A")


if __name__ == "__main__":
    unittest.main()
