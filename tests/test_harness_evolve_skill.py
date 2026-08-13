from __future__ import annotations

from pathlib import Path
import unittest


class HarnessEvolveSkillTests(unittest.TestCase):
    def test_skill_is_repository_backed_and_declares_safe_trigger(self) -> None:
        path = Path(__file__).parents[1] / "skills" / "harness-evolve" / "SKILL.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("name: harness-evolve", text)
        self.assertIn("harness_evolve", text)
        self.assertIn("ordinary workspace", text)

