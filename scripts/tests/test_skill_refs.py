"""参照実在検査: SKILL.md 内の references/*.md / scripts/*.py / templates/*.md への参照を検証する。"""
import os
import re
import unittest

SKILL_MD = os.path.join(os.path.dirname(__file__), "..", "..", "SKILL.md")
BASE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TestSkillRefs(unittest.TestCase):
    """SKILL.md 内のファイル参照が実在するか検査する。"""

    @classmethod
    def setUpClass(cls):
        with open(SKILL_MD, encoding="utf-8") as f:
            cls.content = f.read()

    def _check_refs(self, pattern, prefix=""):
        refs = set(re.findall(pattern, self.content))
        missing = []
        for ref in sorted(refs):
            if ref.startswith("<") and ref.endswith(">"):
                continue  # プレースホルダはスキップ
            path = os.path.join(BASE, prefix, ref) if prefix else os.path.join(BASE, ref)
            if not os.path.isfile(path):
                missing.append(ref)
        self.assertListEqual(missing, [], f"Missing references:\n" + "\n".join(missing))
        return refs

    def test_references_md(self):
        """references/*.md への参照が実在する。"""
        self._check_refs(r"references/([\w.-]+\.md)", prefix="references")

    def test_references_troubleshooting(self):
        """references/troubleshooting.md を参照している。"""
        self.assertIn("references/troubleshooting.md", self.content)

    def test_references_agent_delegation(self):
        """references/agent-delegation.md を参照している。"""
        self.assertIn("references/agent-delegation.md", self.content)

    def test_references_env_windows(self):
        """references/env-windows.md を参照している。"""
        self.assertIn("references/env-windows.md", self.content)

    def test_renamed_docs_forwarding_stubs(self):
        """旧名の転送スタブ（1行）が残り、改名先へ転送している。"""
        for stub, target in (("references/hermes-delegation.md", "agent-delegation.md"),
                             ("references/env-hermes-windows.md", "env-windows.md")):
            path = os.path.join(BASE, stub)
            self.assertTrue(os.path.isfile(path), f"転送スタブが無い: {stub}")
            with open(path, encoding="utf-8") as f:
                text = f.read()
            lines = [ln for ln in text.splitlines() if ln.strip()]
            self.assertEqual(len(lines), 1, f"{stub} は1行の転送スタブであるべき")
            self.assertIn(target, text, f"{stub} が {target} へ転送していない")

    def test_scripts_py(self):
        """scripts/*.py への参照が実在する。"""
        self._check_refs(r"scripts/([\w./-]+\.py)", prefix="scripts")

    def test_templates_md(self):
        """templates/*.md への参照が実在する。"""
        self._check_refs(r"templates/([\w.-]+\.md)", prefix="templates")

    # ── 裸コマンドの再発検査（F1-O3） ──────────────────────────────

    _BACKTICK_RE = re.compile(r"`([^`\n]+)`")
    _FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
    _PY_NAME_RE = re.compile(r"^[A-Za-z0-9_][\w.-]*\.py$")

    def _bare_commands(self, text):
        """バッククォート・コードフェンス内の「裸の *.py で始まるコマンド」を集める。

        ファイル名だけの参照（`` `rename_terms.py` ``）や行番号参照
        （`` saga.py:113 ``）はコマンドではないため対象外。
        """
        offenders = []
        for m in self._BACKTICK_RE.finditer(text):
            head, _, rest = m.group(1).strip().partition(" ")
            if self._PY_NAME_RE.match(head) and rest.strip():
                offenders.append(m.group(1).strip())
        for m in self._FENCE_RE.finditer(text):
            for line in m.group(1).splitlines():
                s = line.strip()
                head, _, rest = s.partition(" ")
                if self._PY_NAME_RE.match(head) and rest.strip():
                    offenders.append(s)
        return offenders

    def test_skill_no_bare_python_commands(self):
        """SKILL.md のコマンド例は python3 scripts/ 前置（裸の *.py で始まらない）。"""
        offenders = self._bare_commands(self.content)
        self.assertListEqual(offenders, [],
                             "裸のコマンドが残っている:\n" + "\n".join(offenders))

    def test_templates_no_bare_python_commands(self):
        """templates/*.md のコマンド例も python3 scripts/ 前置。"""
        tdir = os.path.join(BASE, "templates")
        bad = []
        for name in sorted(os.listdir(tdir)):
            if not name.endswith(".md"):
                continue
            with open(os.path.join(tdir, name), encoding="utf-8") as f:
                for o in self._bare_commands(f.read()):
                    bad.append(f"{name}: {o}")
        self.assertListEqual(bad, [], "裸のコマンドが残っている:\n" + "\n".join(bad))

    def test_translation_input_and_draft_path_documented(self):
        """手順4にコンテキストのパスとドラフト置き場が明記されている（O1/O2 の再発検査）。"""
        self.assertIn("translation_input/{ch}_{b}_translation.md", self.content)
        self.assertIn("output/_work/{ch}_{b}_ja_numbered.md", self.content)
        self.assertNotIn("ドラフトを _work/ に", self.content)


if __name__ == "__main__":
    unittest.main()