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

    def test_references_hermes_delegation(self):
        """references/hermes-delegation.md を参照している。"""
        self.assertIn("references/hermes-delegation.md", self.content)

    def test_scripts_py(self):
        """scripts/*.py への参照が実在する。"""
        self._check_refs(r"scripts/([\w./-]+\.py)", prefix="scripts")

    def test_templates_md(self):
        """templates/*.md への参照が実在する。"""
        self._check_refs(r"templates/([\w.-]+\.md)", prefix="templates")


if __name__ == "__main__":
    unittest.main()