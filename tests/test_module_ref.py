"""module_lookup: full-text search over a campaign's reference/ module text.

Run: python3 -m unittest tests.test_module_ref
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._campaign_fixture import TempCampaign
from tools import module_ref

LEVEL = """# Level One

## 1B The Quiet Halls

Overview of the halls.

**Room Key**

1. Grand Entrance Hall: Dusty hall; Fire Beetles (8) grazing on lichen.
2. Morgue: Rotted wooden shelves for bodies.
"""


class ModuleLookupTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self)
        self.camp.write("reference/02-level-1.md", LEVEL)
        self.lookup = TempCampaign.register(module_ref)["module_lookup"]

    def test_finds_a_keyed_room_with_its_heading_path(self):
        out = self.lookup("fire beetles")
        hit = out["results"][0]
        self.assertIn("Fire Beetles", hit["text"])
        self.assertEqual(hit["path"], "Level One › 1B The Quiet Halls › Room Key")
        self.assertEqual(hit["file"], "02-level-1.md")

    def test_each_numbered_key_entry_is_its_own_result(self):
        out = self.lookup("morgue")
        self.assertEqual(len(out["results"]), 1)
        self.assertTrue(out["results"][0]["text"].startswith("2. Morgue"))

    def test_section_filter(self):
        self.assertEqual(self.lookup("hall", section="Level Two")["results"], [])

    def test_no_reference_dir_says_so(self):
        TempCampaign(self)
        out = TempCampaign.register(module_ref)["module_lookup"]("x")
        self.assertEqual(out["results"], [])
        self.assertIn("note", out)

    def test_bad_fts_syntax_is_quoted_not_raised(self):
        self.assertIsInstance(self.lookup('beetles"')["results"], list)
