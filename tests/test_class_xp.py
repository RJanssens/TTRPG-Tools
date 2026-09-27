"""Multi-class XP: the award splits evenly between the classes (PHB), each
class levels on its own table, and the sheet records per-class levels.

Run: python3 -m unittest tests.test_class_xp
"""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._campaign_fixture import TempCampaign
from tools import class_xp as _xp
from tools import combat, lore, party, world

GRUSK = {"label": "Grusk", "cls": "Fighter/Thief", "race": "bugbear",
         "level": 1, "hp_max": 10, "ac": 6, "thac0": 19}
YSOLDE = {"label": "Ysolde", "cls": "Fighter", "race": "human",
          "level": 1, "hp_max": 10, "ac": 4, "thac0": 20}


class ClassXpTests(unittest.TestCase):
    def test_tables_come_from_the_db(self):
        self.assertEqual(_xp.xp_table("Fighter")[:3], [0, 2000, 4000])
        self.assertEqual(_xp.xp_table("Thief")[:3], [0, 1250, 2500])
        self.assertEqual(_xp.xp_table("Fighter/Thief"), [])

    def test_levels_default_to_the_single_level(self):
        self.assertEqual(_xp.class_levels(GRUSK), {"fighter": 1, "thief": 1})
        self.assertEqual(_xp.level_label(GRUSK), "1/1")
        self.assertEqual(_xp.level_label(YSOLDE), "1")

    def test_stored_levels_win(self):
        g = dict(GRUSK, levels={"fighter": 1, "thief": 2}, level=2)
        self.assertEqual(_xp.level_label(g), "1/2")

    def test_share_is_split_evenly(self):
        rows = {r["class"]: r for r in _xp.progress(GRUSK, 2600)}
        self.assertEqual(rows["Fighter"]["xp"], 1300)
        self.assertFalse(rows["Fighter"]["ready"])
        self.assertTrue(rows["Thief"]["ready"])
        self.assertEqual(rows["Thief"]["total_next"], 2500)
        self.assertEqual(rows["Fighter"]["total_next"], 4000)

    def test_hint_names_only_the_class_that_crossed(self):
        hints = _xp.level_up_hints(GRUSK, 2400, 2600)
        self.assertEqual(len(hints), 1)
        self.assertIn("Thief level 2", hints[0])
        self.assertEqual(_xp.level_up_hints(GRUSK, 1000, 2400), [])

    def test_single_class_hint_uses_the_full_total(self):
        self.assertIn("Fighter level 2", _xp.level_up_hints(YSOLDE, 1900, 2100)[0])

    def test_no_hint_for_a_level_already_recorded(self):
        g = dict(GRUSK, levels={"fighter": 1, "thief": 2}, level=2)
        self.assertEqual(_xp.level_up_hints(g, 2400, 2600), [])

    def test_bar_tracks_the_class_that_levels_soonest(self):
        self.assertEqual(_xp.bar(GRUSK, 338), (0, 2500))
        g = dict(GRUSK, levels={"fighter": 1, "thief": 2}, level=2)
        self.assertEqual(_xp.bar(g, 2600), (0, 4000))
        self.assertEqual(_xp.bar(YSOLDE, 338), (0, 2000))


class _Base(unittest.TestCase):
    def setUp(self):
        combat._session = None
        self.addCleanup(setattr, combat, "_session", None)
        self.camp = TempCampaign(
            self, campaign={"characters": {"grusk": copy.deepcopy(GRUSK),
                                           "ysolde": copy.deepcopy(YSOLDE)}},
            state={"characters": {"grusk": {"xp": 2400}, "ysolde": {"xp": 0}}})
        self.tools = TempCampaign.register(party, world, combat, lore)

    def pc(self, key="grusk"):
        return self.camp.read_json("campaign.json")["characters"][key]


class AwardXpTests(_Base):
    def test_multiclass_award_reports_each_share(self):
        out = self.tools["award_xp"]("grusk", 200)
        self.assertEqual(out["xp_total"], 2600)
        by = {r["class"]: r for r in out["xp_by_class"]}
        self.assertEqual(by["Thief"]["xp"], 1300)
        self.assertTrue(by["Thief"]["ready"])
        self.assertIn("Thief level 2", out["level_up_hint"])
        self.assertNotIn("Fighter", out["level_up_hint"])

    def test_single_class_award_is_unchanged(self):
        out = self.tools["award_xp"]("ysolde", 2000)
        self.assertNotIn("xp_by_class", out)
        self.assertIn("Fighter level 2", out["level_up_hint"])


class UpdateLevelsTests(_Base):
    def test_levels_sets_each_class_and_the_highest_level(self):
        self.tools["update_stats"]("grusk", levels={"Thief": 2, "fighter": 1})
        pc = self.pc()
        self.assertEqual(pc["levels"], {"fighter": 1, "thief": 2})
        self.assertEqual(pc["level"], 2)
        sheet = (self.camp.dir / "characters" / "grusk.md").read_text()
        self.assertIn("Level 1/2", sheet)

    def test_levels_must_name_every_class(self):
        out = self.tools["update_stats"]("grusk", levels={"thief": 2})
        self.assertIn("error", out)

    def test_bare_level_is_refused_for_multiclass(self):
        self.assertIn("error", self.tools["update_stats"]("grusk", level=2))

    def test_levels_is_refused_for_single_class(self):
        self.assertIn("error", self.tools["update_stats"]("ysolde", levels={"fighter": 2}))

    def test_primer_carries_per_class_levels(self):
        self.tools["update_stats"]("grusk", levels={"fighter": 1, "thief": 2})
        party_block = self.tools["session_primer"]()["party"]
        self.assertEqual(party_block["grusk"]["levels"], {"fighter": 1, "thief": 2})
        self.assertNotIn("levels", party_block["ysolde"])


if __name__ == "__main__":
    unittest.main()
