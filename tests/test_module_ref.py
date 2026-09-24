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

    def test_section_filter_applies_before_limit(self):
        # 30 "the"-matching chunks outside the target section, then 8 inside
        # it. If the section filter runs after `LIMIT limit*4` (5*4=20), the
        # in-section rows never make it into the pre-filter window and all
        # of them get dropped. The filter must narrow the SQL match itself.
        noise = "\n\n".join(f"the quiet corridor stretches on, marker {i}."
                             for i in range(30))
        five = "\n\n".join(f"the crypt chamber {i} holds old bones."
                            for i in range(8))
        text = f"# Level One\n\n{noise}\n\n# Level Five\n\n{five}\n"
        self.camp.write("reference/06-level-5.md", text)
        self.lookup = TempCampaign.register(module_ref)["module_lookup"]

        out = self.lookup("the", section="Level Five")
        self.assertEqual(len(out["results"]), 5)
        for hit in out["results"]:
            self.assertEqual(hit["path"], "Level Five")
            self.assertIn("crypt chamber", hit["text"])
