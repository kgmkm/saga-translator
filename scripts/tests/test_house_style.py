"""P0-1: house_style.py の単体テスト。自作フィクスチャのみ使用。"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SCRIPTS))

import house_style  # noqa: E402


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def _valid_rule(**overrides: str | int | list[str]) -> dict:
    """妥当なルール（必要最低限 + 任意オーバーライド）。"""
    base = {
        "id": "test_rule",
        "kind": "naming",
        "scope": "global",
        "pattern": "例",
        "replacement": "例示",
        "status": "candidate",
        "version": 1,
    }
    base.update(overrides)
    return base


def _valid_toml(rules: list[dict]) -> str:
    """妥当な house_style.toml 文字列。"""
    import io
    parts = [
        'version = 1',
        'created_at = "2026-10-03T12:00:00Z"',
        'updated_at = "2026-10-03T12:00:00Z"',
        "",
    ]
    for r in rules:
        parts.append("[[rules]]")
        for k, v in r.items():
            if k == "evidence":
                items = ", ".join(f'"{x}"' for x in v)
                parts.append(f"evidence = [{items}]")
            elif isinstance(v, str):
                parts.append(f'{k} = "{v}"')
            elif isinstance(v, int):
                parts.append(f"{k} = {v}")
        parts.append("")
    return "\n".join(parts)


class TestBuild(unittest.TestCase):
    """build サブコマンド — 作品横断で characters/terms を読んで候補を出す。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())
        self.saga_root = self.td / "_saga"
        self.saga_root.mkdir()

    def _make_work(self, wid: str, chars: list[dict], terms: list[dict]) -> Path:
        """最小の作品ディレクトリを作成。"""
        wd = self.saga_root / wid
        (wd / "analysis").mkdir(parents=True)
        # characters TOML
        if chars:
            lines = []
            for c in chars:
                lines.append("[[characters]]")
                for k, v in c.items():
                    if isinstance(v, str):
                        lines.append(f'{k} = "{v}"')
                    elif isinstance(v, list):
                        items = ", ".join(f'"{x}"' for x in v)
                        lines.append(f"{k} = [{items}]")
                    elif isinstance(v, int):
                        lines.append(f"{k} = {v}")
            (wd / "analysis" / "characters-[ネタバレあり].toml").write_text(
                "\n".join(lines), encoding="utf-8", newline="\n"
            )
        # terms TOML
        if terms:
            lines = []
            for t in terms:
                lines.append("[[terms]]")
                for k, v in t.items():
                    if isinstance(v, str):
                        lines.append(f'{k} = "{v}"')
            (wd / "analysis" / "terms.toml").write_text(
                "\n".join(lines), encoding="utf-8", newline="\n"
            )
        return wd

    def test_build_single_work_generates_candidates(self):
        """1作品から候補が生成される。"""
        self._make_work("12345", chars=[
            {"name_src": "Alice", "name_ja": "アリス", "ruby": "ありす",
             "first_person": "私", "second_person": "あなた"},
        ], terms=[
            {"term_src": "magic", "term_ja": "魔法", "ruby": "まほう"},
        ])
        result = house_style.cmd_build(
            type("Args", (), {"saga_root": str(self.saga_root), "output": None})()
        )
        self.assertEqual(result, 0)

    def test_build_no_work_dir_returns_1(self):
        """作品ディレクトリが無い場合、終了コード 1。"""
        result = house_style.cmd_build(
            type("Args", (), {"saga_root": str(self.saga_root), "output": None})()
        )
        self.assertEqual(result, 1)

    def test_build_outputs_candidate_rules(self):
        """build → stdout に candidate ルールが含まれる。"""
        self._make_work("99999", chars=[
            {"name_src": "Bob", "name_ja": "ボブ", "ruby": "ぼぶ",
             "first_person": "俺"},
        ], terms=[])
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_build(
                type("Args", (), {"saga_root": str(self.saga_root), "output": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("candidate", output)
        self.assertIn("Bob", output)
        self.assertIn("俺", output)

    def test_build_writes_to_file(self):
        """--output でファイルに書き出される。"""
        self._make_work("11111", chars=[
            {"name_src": "Charlie", "name_ja": "チャーリー", "ruby": "ちゃーりー",
             "first_person": "僕"},
        ], terms=[])
        out_path = self.td / "candidates.toml"
        rc = house_style.cmd_build(
            type("Args", (), {"saga_root": str(self.saga_root), "output": str(out_path)})()
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out_path.exists())
        content = out_path.read_text(encoding="utf-8")
        self.assertIn("candidate", content)
        self.assertIn("Charlie", content)

    def test_build_no_characters_file(self):
        """characters TOML が無い作品は素通り。"""
        self._make_work("22222", chars=[], terms=[
            {"term_src": "sword", "term_ja": "剣"},
        ])
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_build(
                type("Args", (), {"saga_root": str(self.saga_root), "output": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("sword", output)

    def test_build_multi_work_aggregates(self):
        """複数作品の分析結果が集約される。"""
        self._make_work("100", chars=[
            {"name_src": "A", "name_ja": "エー", "ruby": "えー"},
        ], terms=[])
        self._make_work("200", chars=[
            {"name_src": "B", "name_ja": "ビー", "ruby": "びー"},
        ], terms=[])
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_build(
                type("Args", (), {"saga_root": str(self.saga_root), "output": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("A", output)
        self.assertIn("B", output)

    def test_build_output_is_valid_toml(self):
        """build の出力がパース可能な TOML である。"""
        import tomllib
        self._make_work("33333", chars=[
            {"name_src": "D", "name_ja": "ディー", "ruby": "でぃー",
             "first_person": "私"},
        ], terms=[
            {"term_src": "ring", "term_ja": "指輪", "ruby": "ゆびわ"},
        ])
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            house_style.cmd_build(
                type("Args", (), {"saga_root": str(self.saga_root), "output": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        # TOML ヘッダ行を無視するための前処理
        toml_lines = [l for l in output.splitlines() if not l.startswith("#")]
        tomllib.loads("\n".join(toml_lines))


class TestValidate(unittest.TestCase):
    """validate サブコマンド — スキーマ適合検査。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def test_valid_toml_returns_0(self):
        """妥当な TOML → 終了コード 0。"""
        p = self.td / "valid.toml"
        p.write_text(_valid_toml([_valid_rule()]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 0)

    def test_missing_version_returns_1(self):
        """version 欠落 → 終了コード 1。"""
        p = self.td / "no_version.toml"
        p.write_text(_valid_toml([_valid_rule()]).replace("version = 1\n", ""), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_missing_rules_returns_1(self):
        """rules 欠落 → 終了コード 1。"""
        p = self.td / "no_rules.toml"
        p.write_text(
            'version = 1\ncreated_at = "2026-01-01T00:00:00Z"\nupdated_at = "2026-01-01T00:00:00Z"\n',
            encoding="utf-8"
        )
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_missing_rule_id_returns_1(self):
        """ルールに id がない → 終了コード 1。"""
        bad = _valid_rule()
        del bad["id"]
        p = self.td / "no_id.toml"
        p.write_text(_valid_toml([bad]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_duplicate_id_returns_1(self):
        """重複 id → 終了コード 1。"""
        rule = _valid_rule(id="dup")
        p = self.td / "dup_id.toml"
        p.write_text(_valid_toml([rule, rule]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_invalid_kind_returns_1(self):
        """未知の kind → 終了コード 1。"""
        p = self.td / "bad_kind.toml"
        p.write_text(_valid_toml([_valid_rule(kind="unknown_kind")]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_invalid_scope_returns_1(self):
        """未知の scope → 終了コード 1。"""
        p = self.td / "bad_scope.toml"
        p.write_text(_valid_toml([_valid_rule(scope="universe")]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_invalid_status_returns_1(self):
        """未知の status → 終了コード 1。"""
        p = self.td / "bad_status.toml"
        p.write_text(_valid_toml([_valid_rule(status="rejected")]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_scope_work_without_target_returns_1(self):
        """scope=work で target がない → 終了コード 1。"""
        p = self.td / "no_target.toml"
        p.write_text(_valid_toml([_valid_rule(scope="work")]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_scope_global_without_target_returns_0(self):
        """scope=global で target なし → 許容（終了コード 0）。"""
        p = self.td / "global_ok.toml"
        p.write_text(_valid_toml([_valid_rule(scope="global")]), encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 0)

    def test_both_replacement_and_preference_returns_1(self):
        """replacement と preference 両方指定 → 終了コード 1。"""
        p = self.td / "both.toml"
        p.write_text(
            _valid_toml([_valid_rule(replacement="A", preference="B")]),
            encoding="utf-8"
        )
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_non_existent_file_returns_1(self):
        """存在しないファイル → 終了コード 1。"""
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(self.td / "nonexistent.toml")})()
        )
        self.assertEqual(rc, 1)

    def test_malformed_toml_returns_1(self):
        """TOML 構文エラー → 終了コード 1。"""
        p = self.td / "bad_syntax.toml"
        p.write_text('version = "unclosed\n', encoding="utf-8")
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)

    def test_version_not_int_returns_1(self):
        """version が整数でない → 終了コード 1。"""
        p = self.td / "version_str.toml"
        p.write_text(
            _valid_toml([_valid_rule(version="one")]),
            encoding="utf-8"
        )
        rc = house_style.cmd_validate(
            type("Args", (), {"toml_path": str(p)})()
        )
        self.assertEqual(rc, 1)


class TestGet(unittest.TestCase):
    """get サブコマンド — フィルタ抽出。"""

    def setUp(self):
        self.td = Path(tempfile.mkdtemp())

    def _make_rules_toml(self, rules: list[dict]) -> str:
        return _valid_toml(rules)

    def test_get_all_rules(self):
        """フィルタなし → 全ルール。"""
        p = self.td / "all.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="r1", scope="global", kind="naming", status="approved"),
            _valid_rule(id="r2", scope="work", kind="ruby", status="candidate", target="123"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": None, "kind": None,
                                   "status": None, "target": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("r1", output)
        self.assertIn("r2", output)

    def test_get_filter_scope(self):
        """--scope で絞り込み。"""
        p = self.td / "scope_filter.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="g1", scope="global", kind="naming", status="approved"),
            _valid_rule(id="w1", scope="work", kind="goi", status="candidate",
                        target="123", pattern="foo", replacement="bar"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": "global", "kind": None,
                                   "status": None, "target": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("g1", output)
        self.assertNotIn("w1", output)

    def test_get_filter_kind(self):
        """--kind で絞り込み。"""
        p = self.td / "kind_filter.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="n1", kind="naming", scope="global", status="approved"),
            _valid_rule(id="r1", kind="ruby", scope="character", status="candidate",
                        target="Alice", pattern="Alice", replacement="{Alice|ありす}"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": None, "kind": "ruby",
                                   "status": None, "target": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("r1", output)
        self.assertNotIn("n1", output)

    def test_get_filter_status(self):
        """--status で絞り込み。"""
        p = self.td / "status_filter.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="c1", status="candidate", scope="global", kind="naming"),
            _valid_rule(id="a1", status="approved", scope="global", kind="naming"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": None, "kind": None,
                                   "status": "approved", "target": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("a1", output)
        self.assertNotIn("c1", output)

    def test_get_filter_combined(self):
        """scope + kind + status の組み合わせ。"""
        p = self.td / "combined.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="t1", scope="work", kind="naming", status="approved",
                        target="999", pattern="X", replacement="Y"),
            _valid_rule(id="t2", scope="global", kind="naming", status="approved"),
            _valid_rule(id="t3", scope="work", kind="ruby", status="approved",
                        target="999", pattern="A", replacement="{A|a}"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": "work", "kind": "naming",
                                   "status": "approved", "target": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("t1", output)
        self.assertNotIn("t2", output)
        self.assertNotIn("t3", output)

    def test_get_filter_target(self):
        """--target で絞り込み。"""
        p = self.td / "target_filter.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="x1", scope="work", kind="naming", status="approved",
                        target="111", pattern="A", replacement="B"),
            _valid_rule(id="x2", scope="work", kind="goi", status="candidate",
                        target="222", pattern="C", replacement="D"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": None, "kind": None,
                                   "status": None, "target": "111"})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertIn("x1", output)
        self.assertNotIn("x2", output)

    def test_get_non_existent_file_returns_1(self):
        """存在しないファイル → 終了コード 1。"""
        rc = house_style.cmd_get(
            type("Args", (), {"toml_path": str(self.td / "nonexistent.toml"),
                               "scope": None, "kind": None, "status": None, "target": None})()
        )
        self.assertEqual(rc, 1)

    def test_get_no_match_returns_empty(self):
        """該当なし → 空出力（終了コード 0）。"""
        p = self.td / "no_match.toml"
        p.write_text(self._make_rules_toml([
            _valid_rule(id="m1", scope="global", kind="naming", status="approved"),
        ]), encoding="utf-8")
        import io
        old_stdout = sys.stdout
        sys.stdout = io.StringIO()
        try:
            rc = house_style.cmd_get(
                type("Args", (), {"toml_path": str(p), "scope": "work", "kind": None,
                                   "status": None, "target": None})()
            )
            output = sys.stdout.getvalue()
        finally:
            sys.stdout = old_stdout
        self.assertEqual(rc, 0)
        self.assertEqual(output.strip(), "")


if __name__ == "__main__":
    unittest.main()