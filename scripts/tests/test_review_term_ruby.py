"""review の用語初出ルビ（リスク項目＋抜き取りの強制追加）のテスト（8）。

- terms.toml で ruby を持つ用語について、対象ブロックを順に原文走査し、最初に
  term_src が現れるブロックを「<用語> 初出（用語ルビ）」としてリスク項目に追加
  （detail は ruby=<読み>。対象外ブロックの初出は出さない）
- ruby を持たない用語は出さない
- 抜き取り（_sample_paragraphs）は固定 seed のランダム部を一切変えず、末尾に
  kind="用語初出" として初出段落を決定的に追記する（追記であることを固定）

合成データのみ使用。作品固有情報は含めない。
"""
from __future__ import annotations

import contextlib
import io
import json
import random
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import _lib  # noqa: E402

_TERMS = [
    {"term_src": "灵気", "term_ja": "霊気", "ruby": "れいき"},
    {"term_src": "丹药", "term_ja": "丹薬"},                      # ruby 無し
    {"term_src": "神識", "term_ja": "神識", "ruby": "しんしき"},
    {"term_src": "幻獣", "term_ja": "幻獣", "ruby": "げんじゅう"},  # 対象ブロックに無い
]

# b1(1-2) は未確定（対象外）。灵気の初出は b1 P1 だが、対象内の初出は b2 P4。
_PARAS = [
    {"n": 1, "text": "第一段落。灵気が満ちている。"},
    {"n": 2, "text": "第二段落。"},
    {"n": 3, "text": "第三段落。丹药を飲む。"},
    {"n": 4, "text": "第四段落。灵気を感じる。"},
    {"n": 5, "text": "第五段落。灵気が溢れ、神識が澄む。"},
    {"n": 6, "text": "第六段落。"},
]

# 期待値: 霊気の初出 = ch001/b2 P4、神識の初出 = ch001/b3 P5
_EXPECTED_FORCED = [
    {"block": "ch001/b2", "para": 4, "text": "第四段落。灵気を感じる。", "kind": "用語初出"},
    {"block": "ch001/b3", "para": 5, "text": "第五段落。灵気が溢れ、神識が澄む。", "kind": "用語初出"},
]


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _make_work(base: Path, name: str = "synth_term_work", terms: list[dict] | None = None) -> Path:
    """合成の review 用 work（用語つき）。確定出力はマーカー無し。"""
    work = base / name
    _write(work / "manifest.toml", 'title = "合成作品"\nlang = "zh"\ndst = "ja"\n')
    _write(work / "chapters_meta.toml",
           "[[chapters]]\n"
           'id = "ch001"\n'
           'blocks = [{id="b1", paras=[1,2]}, {id="b2", paras=[3,4]}, {id="b3", paras=[5,6]}]\n')
    _lib.save_paras(work, "ch001", {"paras": [dict(p) for p in _PARAS]})
    _write(work / "analysis" / "characters-[ネタバレあり].toml", "characters = []\n")
    _lib.save_toml(work / "analysis" / "terms.toml",
                   {"terms": [dict(t) for t in (terms if terms is not None else _TERMS)]})
    _write(work / "analysis" / "plot-[ネタバレあり].toml", "entries = []\n")
    # 確定ブロック出力（b2 / b3 のみ。b1 は未確定 = 対象外）
    _write(work / "output" / "ch001" / "b2_ja.md", "訳二の甲。\n\n訳二の乙。\n")
    _write(work / "output" / "ch001" / "b3_ja.md", "訳三の甲。\n\n訳三の乙。\n")
    return work


def _run_review(work: Path, wave: int = 1, sample_ratio: float = 0.5,
                json_output: bool = False) -> tuple[int, str]:
    """cmd_review を呼び、終了コードと stdout+stderr を返す。"""
    from saga import cmd_review
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        code = cmd_review(work, wave=wave, sample_ratio=sample_ratio,
                          json_output=json_output)
    return int(code or 0), buf.getvalue()


def _read_review(work: Path, wave: int = 1) -> str:
    return (work / "_work" / f"review_wave{wave}.md").read_text(encoding="utf-8")


def _sampling_section(content: str) -> str:
    out: list[str] = []
    inside = False
    for line in content.splitlines():
        if line.startswith("## 3."):
            inside = True
            continue
        if inside and line == "---":
            break
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def _selection_line(content: str) -> str:
    m = re.search(r"^選定: (.+)$", content, re.M)
    return m.group(1) if m else ""


def _risk_lines(content: str) -> list[str]:
    out: list[str] = []
    inside = False
    for line in content.splitlines():
        if line.startswith("## 2."):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside and line.startswith("- "):
            out.append(line)
    return out


def _expected_random_part(work: Path, blocks: list[tuple[str, str]],
                          ratio: float, seed: str) -> list[dict]:
    """変更前の実装（seed 固定の rng.sample）を独立に再現したランダム部。"""
    rng = random.Random(seed)
    out: list[dict] = []
    for cid, bid in blocks:
        paras = _lib.load_paras(work, cid).get("paras", [])
        meta = _lib.load_chapters_meta(work)
        ch = next((c for c in meta.get("chapters", []) if c["id"] == cid), None)
        blk = next((b for b in (ch or {}).get("blocks", []) if b["id"] == bid), None)
        if not blk:
            continue
        ps, pe = blk.get("paras", (1, len(paras)))
        block_paras = [p for p in paras if ps <= p.get("n", 0) <= pe]
        n = max(1, int(len(block_paras) * ratio))
        chosen = rng.sample(block_paras, min(n, len(block_paras)))
        for p in chosen:
            out.append({"block": f"{cid}/{bid}", "para": p.get("n", 0),
                        "text": p.get("text", "")})
    return out


class TestReviewTermRuby(unittest.TestCase):
    """review: 用語初出ルビのリスク項目と抜き取りの強制追加。"""

    maxDiff = None

    def setUp(self):
        self.td = Path(tempfile.mkdtemp(prefix="q8_term_ruby_"))
        self.work = _make_work(self.td)
        self.addCleanup(shutil.rmtree, self.td, ignore_errors=True)

    def _blocks(self) -> list[tuple[str, str]]:
        from saga import _list_committed_blocks
        return _list_committed_blocks(self.work)

    def test_risk_items_only_ruby_terms_and_first_target_block(self):
        """ruby 付き用語の初出ブロックだけが出る（対象外ブロック・ruby 無しは出ない）。"""
        code, out = _run_review(self.work, wave=1, sample_ratio=0.5)
        self.assertEqual(code, 0, out)
        content = _read_review(self.work)
        risk = _risk_lines(content)
        self.assertEqual(risk, [
            "- ch001/b2 霊気 初出（用語ルビ）: ruby=れいき",
            "- ch001/b3 神識 初出（用語ルビ）: ruby=しんしき",
        ])
        # 対象外ブロック（b1）の初出は出さない・ruby 無し用語は出さない
        self.assertNotIn("ch001/b1", "\n".join(risk))
        self.assertNotIn("丹薬", content)
        self.assertNotIn("幻獣", content)

    def test_forced_samples_appended_after_unchanged_random_part(self):
        """ランダム部は変更前と同一・強制分（用語初出）は末尾に決定的に追記。"""
        from saga import _review_seed, _sample_paragraphs
        blocks = self._blocks()
        seed = _review_seed(self.work.name, 1)
        samples = _sample_paragraphs(self.work, blocks, 0.5, seed)
        # ランダム部（kind 無し）は変更前の実装と一致する
        random_part = [s for s in samples if not s.get("kind")]
        self.assertEqual(random_part, _expected_random_part(self.work, blocks, 0.5, seed))
        self.assertTrue(random_part, "ランダム部が空（比較が自明になる）")
        # 強制分は末尾にあり、決定的（同条件で同値）
        self.assertEqual(samples[-2:], _EXPECTED_FORCED)
        self.assertEqual([s for s in samples if s.get("kind")], _EXPECTED_FORCED)
        self.assertEqual(samples, _sample_paragraphs(self.work, blocks, 0.5, seed))

    def test_forced_append_even_when_ratio_covers_all_paragraphs(self):
        """ratio=1.0 でランダム部が全段落を覆っても、強制分は末尾に追記される。"""
        from saga import _review_seed, _sample_paragraphs
        blocks = self._blocks()
        samples = _sample_paragraphs(self.work, blocks, 1.0, _review_seed(self.work.name, 1))
        self.assertEqual(samples[-2:], _EXPECTED_FORCED)
        self.assertEqual([s for s in samples if s.get("kind")], _EXPECTED_FORCED)

    def test_markdown_and_json_show_kind_and_determinism(self):
        """検収リストに（用語初出）が出て、JSON の kind も末尾に載る。再実行で同一。"""
        code, _ = _run_review(self.work, wave=1, sample_ratio=0.5)
        self.assertEqual(code, 0)
        content = _read_review(self.work)
        self.assertIn("### ch001/b2 P4（用語初出）", content)
        self.assertIn("### ch001/b3 P5（用語初出）", content)
        # 選定の末尾が強制分（追記であることを固定）
        self.assertTrue(_selection_line(content).endswith("ch001/b2 P4, ch001/b3 P5"),
                        _selection_line(content))
        s1 = _sampling_section(content)

        code, out = _run_review(self.work, wave=1, sample_ratio=0.5, json_output=True)
        self.assertEqual(code, 0, out)
        data = json.loads(out)
        kinds = [s.get("kind") for s in data["sample_items"]]
        self.assertEqual(kinds[-2:], ["用語初出", "用語初出"])
        self.assertNotIn("用語初出", kinds[:-2])
        self.assertEqual(data["sample_items"][-2:], _EXPECTED_FORCED)

        code, _ = _run_review(self.work, wave=1, sample_ratio=0.5)
        self.assertEqual(code, 0)
        self.assertEqual(_sampling_section(_read_review(self.work)), s1)

    def test_no_ruby_terms_no_forced(self):
        """ruby を持たない用語だけなら、初出ルビも強制分も出ない。"""
        work = _make_work(self.td, "synth_no_ruby", terms=[
            {"term_src": "灵気", "term_ja": "霊気"},
            {"term_src": "神識", "term_ja": "神識"},
        ])
        code, out = _run_review(work, wave=1, sample_ratio=0.5)
        self.assertEqual(code, 0, out)
        content = _read_review(work)
        self.assertNotIn("用語ルビ", content)
        self.assertNotIn("用語初出", content)

    def test_risk_items_for_terms_not_in_target_blocks_absent(self):
        """対象ブロックに現れない ruby 付き用語はリスクに出ない（幻獣のみの work）。"""
        work = _make_work(self.td, "synth_absent", terms=[
            {"term_src": "幻獣", "term_ja": "幻獣", "ruby": "げんじゅう"},
        ])
        code, out = _run_review(work, wave=1, sample_ratio=0.5)
        self.assertEqual(code, 0, out)
        content = _read_review(work)
        self.assertNotIn("用語ルビ", content)
        self.assertNotIn("用語初出", content)


if __name__ == "__main__":
    unittest.main()
