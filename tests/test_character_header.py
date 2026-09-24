"""The canonical mechanics header on get_character.

Run: python3 -m unittest tests.test_character_header
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import world


class MechanicsHeaderTests(unittest.TestCase):
    def test_race_is_in_the_identity_line(self):
        # "Quillory is a satyr, no?" — the DM had ruled on class alone.
        out = world._mechanics_block({"race": "satyr", "cls": "Bard", "level": 2})
        self.assertIn("Satyr Bard", out)

    def test_no_race_leaves_class_alone(self):
        out = world._mechanics_block({"cls": "Fighter", "level": 1})
        self.assertIn("> Fighter · Level 1", out)


if __name__ == "__main__":
    unittest.main()
