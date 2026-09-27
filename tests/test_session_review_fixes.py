"""Fixes from the curse-of-stonehell session review.

  * monster_lookup("bat") returned the werebat — first substring hit by id.
  * joke monsters (Dragon's April issues) needed hiding without losing the row.
  * NPCs from introduce_npc were invisible to hire() and reaction(npc=).
  * every PC fought as size M, so a pixie took the wrong die and never got
    her +2 AC against bigger attackers.
  * monsters at 0 hp were "unconscious/bleeding"; combat_round swung at them.
  * the wiki importer read only the first plain infobox of a page.

Run: python3 -m unittest tests.test_session_review_fixes
"""

from __future__ import annotations

import random
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import _campaign as _c
from tests._campaign_fixture import TempCampaign
from tools import combat, dice, hirelings, lookup, world
from tools import import_wiki_monsters as wiki
from tools import soft_delete_monsters as sdm


def _monster_db(names_hd: list[tuple[str, str]]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE monsters (id INTEGER PRIMARY KEY, name TEXT, hit_dice TEXT)")
    conn.executemany("INSERT INTO monsters (name, hit_dice) VALUES (?, ?)", names_hd)
    return conn


class MonsterMatchTests(unittest.TestCase):
    def setUp(self):
        # The werebat has the lowest id, as in the real DB.
        self.conn = _monster_db([
            ("lycanthrope, werebat", "4+2"), ("bat, huge", "4-6"),
            ("bat, common", "1-2 hp"), ("bat (various)", ""), ("batracine", "2+2"),
            ("red dragon", "11"), ("combat dog", "2"),
        ])
        self.addCleanup(self.conn.close)

    def names(self, q):
        return [r["name"] for r in lookup.find_monster_rows(self.conn, q)]

    def test_bat_is_the_common_bat_not_the_werebat(self):
        self.assertEqual(self.names("bat")[0], "bat, common")
        self.assertLess(self.names("bat").index("bat, huge"),
                        self.names("bat").index("lycanthrope, werebat"))

    def test_statless_family_row_ranks_below_statted_siblings(self):
        got = self.names("bat")
        self.assertLess(got.index("bat, huge"), got.index("bat (various)"))

    def test_exact_name_wins(self):
        self.assertEqual(self.names("batracine")[0], "batracine")

    def test_word_order_fallback(self):
        self.assertEqual(self.names("dragon, red"), ["red dragon"])

    def test_soft_deleted_rows_are_invisible(self):
        sdm.ensure_columns(self.conn)
        done, _ = sdm.soft_delete(self.conn, ["bat, common"], "test")
        self.assertEqual(done, ["bat, common"])
        self.assertNotIn("bat, common", self.names("bat"))
        sdm.restore(self.conn, ["bat, common"])
        self.assertEqual(self.names("bat")[0], "bat, common")

    def test_db_without_deleted_column_still_works(self):
        self.assertEqual(lookup.active_monster_clause(self.conn), "1=1")


class NpcRegistryTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self, campaign={
            "characters": {"korrag": {"label": "Korrag", "hp_max": 13,
                                      "ability_scores": {"cha": 14}}},
            "default_character": "korrag",
        })
        self.camp.write("characters/persis.md", "# Persis Nighthollow\n*Human — guard*\n")
        self.camp.write("characters/korrag.md", "# Korrag\n")
        self.tools = TempCampaign.register(dice, hirelings, world)

    def test_file_only_npc_can_be_hired_and_rolled(self):
        r = self.tools["reaction"](npc="persis")
        self.assertNotIn("error", r)
        h = self.tools["hire"]("persis", "korrag", "1 gp/day")
        self.assertNotIn("error", h)
        self.assertEqual(h["label"], "Persis Nighthollow")
        npc = self.camp.read_json("campaign.json")["npcs"]["persis"]
        self.assertTrue(npc["is_hireling"])
        self.assertNotIn("error", self.tools["loyalty_check"]("persis"))

    def test_pcs_are_never_registered_as_npcs(self):
        cfg = _c.load_campaign()
        self.assertEqual(_c.resolve_npc(cfg, "korrag"), (None, None))

    def test_introduce_npc_registers(self):
        with mock.patch("tools.images.generate_portrait_for", create=True):
            out = self.tools["introduce_npc"]("human", "female", "porter")
        npcs = self.camp.read_json("campaign.json")["npcs"]
        self.assertEqual(npcs[out["slug"]]["label"], out["name"])

    def test_stub_never_overwrites_the_prose_sheet(self):
        _c.resolve_npc(_c.load_campaign(), "persis")
        self.assertIn("error", self.tools["refresh_sheet"]("persis"))
        self.assertIn("error", self.tools["update_stats"]("persis", ac=5))
        self.assertIn("guard", (self.camp.dir / "characters/persis.md").read_text())


class SizeAndDeathTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self, campaign={"characters": {
            "thistle": {"label": "Thistle", "race": "pixie", "hp_max": 4, "ac": 6,
                        "thac0": 20, "ac_vs_larger": 2,
                        "attacks": [{"name": "Dagger", "thac0": 20, "speed": 2,
                                     "attacks": 1, "damage_sm": "1d4", "damage_l": "1d3"}]},
            "korrag": {"label": "Korrag", "race": "minotaur", "hp_max": 13, "ac": 4,
                       "thac0": 19,
                       "attacks": [{"name": "Axe", "thac0": 19, "speed": 7,
                                    "attacks": 1, "damage_sm": "1d8", "damage_l": "1d8"}]},
        }})
        self.t = TempCampaign.register(combat)
        self.t["start_combat"]()
        self.t["add_combatant"]("Goblin", hp=5, ac=6, thac0=20, dmg="1d6", size="S")
        self.t["add_combatant"]("Ogre", hp=20, ac=5, thac0=17, dmg="1d10", size="L")

    def test_race_sets_pc_size(self):
        by = {c["name"]: c for c in combat._session["combatants"]}
        self.assertEqual(by["Thistle"]["size"], "S")
        self.assertEqual(by["Korrag"]["size"], "L")

    def test_ac_vs_larger_applies_only_to_bigger_attackers(self):
        random.seed(1)
        vs_ogre = [self.t["attack"]("Ogre", "Thistle") for _ in range(3)]
        self.assertTrue(all(any("+2 AC" in n for n in r["notes"])
                            for r in vs_ogre if r["roll"] not in (1, 20)))
        vs_gob = self.t["attack"]("Goblin", "Thistle")
        self.assertFalse(any("+2 AC" in n for n in vs_gob["notes"]))

    def test_monster_dies_at_zero(self):
        gob = combat._find_combatant("Goblin")
        gob["hp"] = 0
        self.assertTrue(combat._is_dead(gob))
        self.assertEqual(self.t["combat_status"]()["combatants"][2]["status"], "dead")
        pc = combat._find_combatant("Thistle")
        pc["hp"] = 0
        self.assertFalse(combat._is_dead(pc))

    def test_combat_round_skips_dead_targets(self):
        combat._find_combatant("Goblin")["hp"] = 0
        out = self.t["combat_round"]([{"attacker": "Korrag", "target": "Goblin"}],
                                     order="given")
        act = out["actions"][0]
        self.assertIn("target already dead", act["skipped"])
        self.assertEqual(act["living_foes"], ["Ogre"])


class WikiParsingTests(unittest.TestCase):
    NUMBERED = """{{Creature
| name = Baboon
| source1 = MC2
| name1 = Wild Baboon
| name2 = Banderlog
| hitdice1 = 1+1
| hitdice2 = 4
| thac01 = 18
| thac02 = 15
}}
'''Baboons''' are monkeys."""

    TABLE = """{{Creature
| name = Bat (Various)
| source = Monstrous Manual
}}
{| class="article-table"
!
! Common
! Night Hunter
|-
! Hit Dice:
| 1-2&nbsp;hp
| 2+2
|-
! THAC0:
| 20
| 19
|-
! Size:
| T (1')
| M (7')
|}
'''Bats''' are animals."""

    def test_variant_names(self):
        self.assertEqual(wiki.variant_name("bat (various)", "Common"), "bat, common")
        self.assertEqual(wiki.variant_name("baboon", "Wild Baboon"), "wild baboon")
        self.assertEqual(wiki.variant_name("ant", "Giant"), "ant, giant")

    def test_thac0_index_split(self):
        self.assertEqual(wiki._indexed_key("thac01"), ("thac0", "1"))
        self.assertEqual(wiki._indexed_key("thac0"), ("thac0", ""))
        self.assertEqual(wiki._indexed_key("hitdice12"), ("hitdice", "12"))

    def test_numbered_infobox(self):
        rows = {r["name"]: r for r in wiki.build_rows("Baboon", self.NUMBERED)}
        self.assertEqual(rows["wild baboon"]["hit_dice"], "1+1")
        self.assertEqual(rows["baboon, banderlog"]["thac0"], 15)

    def test_article_table(self):
        rows = {r["name"]: r for r in wiki.build_rows("Bat", self.TABLE)}
        self.assertEqual(rows["bat, common"]["hit_dice"], "1-2 hp")
        self.assertEqual(rows["bat, night hunter"]["size"], "M (7')")
        self.assertEqual(rows["bat, night hunter"]["thac0"], 19)
        self.assertEqual(rows["bat (various)"]["hit_dice"], None)

    def test_fill_stats_only_touches_empty_rows(self):
        with tempfile.TemporaryDirectory() as d:
            conn = sqlite3.connect(Path(d) / "m.db")
            conn.execute("CREATE TABLE monsters (id INTEGER PRIMARY KEY, name TEXT, "
                         "hit_dice TEXT, thac0 INTEGER, size TEXT, description TEXT)")
            conn.execute("INSERT INTO monsters VALUES (1, 'acid pigeon', NULL, NULL, NULL, 'kept')")
            row = {c: None for c in wiki._ALL_COLUMNS}
            row.update(name="acid pigeon", hit_dice="1-4 hp", thac0=20, size="S")
            with mock.patch.object(wiki, "_STAT_COLUMNS", ("hit_dice", "thac0", "size")):
                self.assertEqual(wiki.fill_stats(conn, row), 1)
            self.assertEqual(conn.execute("SELECT hit_dice, thac0, description FROM monsters")
                             .fetchone(), ("1-4 hp", 20, "kept"))
            conn.close()


if __name__ == "__main__":
    unittest.main()
