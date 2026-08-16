from pathlib import Path
import unittest


ROOT = Path(__file__).parents[1]


class HarnessEvolveSkillTests(unittest.TestCase):
    def test_harness_skills_are_isolated_from_main_catalog(self) -> None:
        self.assertFalse((ROOT / "skills" / "harness-evolve").exists())
        self.assertFalse((ROOT / "skills" / "harness-capability").exists())
        resource = ROOT / "harness-evolution" / "runtime" / "skills"
        expected = {
            "repository-safety",
            "validated-repair",
            "hook-evolution",
            "runtime-failure-repair",
            "tool-capability-evolution",
            "conservative-dream-review",
        }
        found = {path.parent.name for path in resource.rglob("SKILL.md")}
        self.assertEqual(found, expected)


if __name__ == "__main__":
    unittest.main()
