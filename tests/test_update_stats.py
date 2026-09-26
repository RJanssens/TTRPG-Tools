"""update_stats: change a PC's or NPC's stored mechanics in place — AC after
new armour, THAC0/HP/saves at level-up — without re-creating the sheet.

Run: python3 -m unittest tests.test_update_stats
"""

from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._campaign_fixture import TempCampaign
from tools import combat, world

YSOLDE = {
    "label": "Ysolde", "cls": "Fighter", "race": "human", "level": 3,
    "hp_max": 24, "ac": 4, "thac0": 18,
    "attacks": [
        {"name": "Long sword", "thac0": 17, "damage_sm": "1d8+1",
         "damage_l": "1d12+1", "speed": 5, "attacks": 1},
        {"name": "Dagger", "thac0": 18, "damage_sm": "1d4", "damage_l": "1d3",
         "speed": 2, "attacks": 1},
    ],
    "inventory": ["Long sword", "Dagger", "Chain mail"],
}
CAMPAIGN = {
    "characters": {"ysolde": YSOLDE},
    "npcs": {"brannoc": {"label": "Brannoc", "slug": "brannoc", "cls": "Fighter",
                         "level": 1, "hp_max": 8, "ac": 7, "thac0": 20}},
}


class _Base(unittest.TestCase):
    def setUp(self):
        combat._session = None
        self.addCleanup(setattr, combat, "_session", None)
        self.camp = TempCampaign(self, campaign=copy.deepcopy(CAMPAIGN),
                                 state={"characters": {"ysolde": {"hp": 20}}})
        self.camp.write("characters/brannoc.md", "# Brannoc\n")
        self.tools = TempCampaign.register(world, combat)
        self.update = self.tools["update_stats"]

    def pc(self, key="ysolde"):
        return self.camp.read_json("campaign.json")["characters"][key]


class UpdatePcTests(_Base):
    def test_changes_only_the_fields_passed(self):
        out = self.update("Ysolde", ac=2, reason="plate mail and shield")
        self.assertEqual(out["changes"], {"ac": [4, 2]})
        pc = self.pc()
        self.assertEqual(pc["ac"], 2)
        for field in ("thac0", "hp_max", "attacks", "inventory", "level"):
            self.assertEqual(pc[field], YSOLDE[field], field)

    def test_accepts_zero_and_negative_ac(self):
        self.update("ysolde", ac=0)
        self.assertEqual(self.pc()["ac"], 0)
        self.update("ysolde", ac=-2)
        self.assertEqual(self.pc()["ac"], -2)

    def test_label_prefix_resolves(self):
        self.update("ysol", thac0=17)
        self.assertEqual(self.pc()["thac0"], 17)

    def test_thac0_alone_warns_when_attacks_carry_their_own(self):
        out = self.update("ysolde", thac0=16)
        self.assertIn("attacks[]", out["warnings"][0])
        attacks = copy.deepcopy(YSOLDE["attacks"])
        attacks[0]["thac0"] = 15
        self.assertNotIn("warnings", self.update("ysolde", thac0=15, attacks=attacks))

    def test_logs_an_event_with_old_and_new_values(self):
        self.update("ysolde", ac=2, reason="plate mail and shield")
        ev = self.camp.events()[-1]
        self.assertEqual(ev["type"], "stats_update")
        self.assertEqual(ev["character"], "ysolde")
        self.assertEqual(ev["changes"], {"ac": [4, 2]})
        self.assertIn("plate mail", ev["text"])
        self.assertIn("AC 4 → 2", ev["text"])

    def test_rewrites_the_sheet(self):
        self.update("ysolde", ac=2)
        sheet = (self.camp.dir / "characters" / "ysolde.md").read_text()
        self.assertIn("**AC:** 2", sheet)
        self.assertTrue(sheet.startswith("# Ysolde"))

    def test_ac_zero_appears_on_the_sheet(self):
        self.update("ysolde", ac=0)
        sheet = (self.camp.dir / "characters" / "ysolde.md").read_text()
        self.assertIn("**AC:** 0", sheet)

    def test_no_fields_is_an_error_and_writes_nothing(self):
        out = self.update("ysolde", reason="nothing")
        self.assertIn("error", out)
        self.assertEqual(self.camp.events(), [])

    def test_unchanged_value_is_not_a_change(self):
        out = self.update("ysolde", ac=4)
        self.assertEqual(out["changes"], {})
        self.assertEqual(self.camp.events(), [])

    def test_unknown_character_is_an_error(self):
        self.assertIn("error", self.update("nobody", ac=2))

    def test_saves_are_normalised(self):
        self.update("ysolde", saves=[13, 15, 14, 16, 16])
        saves = self.pc()["saves"]
        self.assertEqual(saves[0], {"type": "paralysis_poison_death", "value": 13})
        self.assertEqual(len(saves), 5)

    def test_bad_saves_are_rejected(self):
        self.assertIn("error", self.update("ysolde", saves=["nonsense"]))
        self.assertNotIn("saves", self.pc())

    def test_lowering_hp_max_clamps_current_hp(self):
        out = self.update("ysolde", hp_max=15)
        self.assertEqual(self.camp.read_json("state.json")["characters"]["ysolde"]["hp"], 15)
        self.assertEqual(out["hp_current"], 15)

    def test_raising_hp_max_leaves_current_hp(self):
        self.update("ysolde", hp_max=32)
        self.assertEqual(self.camp.read_json("state.json")["characters"]["ysolde"]["hp"], 20)


class UpdateNpcTests(_Base):
    def test_updates_an_npc_stat_block(self):
        out = self.update("brannoc", ac=0)
        self.assertEqual(out["changes"], {"ac": [7, 0]})
        npc = self.camp.read_json("campaign.json")["npcs"]["brannoc"]
        self.assertEqual(npc["ac"], 0)
        self.assertEqual(npc["thac0"], 20)

    def test_npc_without_a_stat_block_points_at_set_npc_stats(self):
        self.camp.write("characters/wenna.md", "# Wenna\n")
        out = self.update("wenna", ac=5)
        self.assertIn("set_npc_stats", out["error"])


class CombatSyncTests(_Base):
    def setUp(self):
        super().setUp()
        self.tools["start_combat"]()

    def combatant(self):
        return next(c for c in combat.get_session()["combatants"] if c.get("_key") == "ysolde")

    def test_ac_reaches_the_running_fight(self):
        out = self.update("ysolde", ac=2)
        self.assertTrue(out["combat_synced"])
        self.assertEqual(self.combatant()["ac"], 2)
        on_disk = json.loads((self.camp.dir / "combat_session.json").read_text())
        c = next(c for c in on_disk["combatants"] if c.get("_key") == "ysolde")
        self.assertEqual(c["ac"], 2)

    def test_new_attacks_keep_the_active_weapon(self):
        c = self.combatant()
        c["weapon"] = "Dagger"
        attacks = copy.deepcopy(YSOLDE["attacks"])
        attacks[1]["thac0"] = 16
        self.update("ysolde", attacks=attacks)
        c = self.combatant()
        self.assertEqual(c["weapon"], "Dagger")
        self.assertEqual(c["thac0"], 16)

    def test_lower_hp_max_clamps_combat_hp(self):
        self.update("ysolde", hp_max=12)
        c = self.combatant()
        self.assertEqual(c["hp_max"], 12)
        self.assertEqual(c["hp"], 12)

    def test_no_fight_means_no_sync(self):
        combat._session = None
        (self.camp.dir / "combat_session.json").unlink()
        self.assertFalse(self.update("ysolde", ac=2)["combat_synced"])


class SetNpcStatsZeroTests(_Base):
    def test_ac_zero_is_stored(self):
        self.tools["set_npc_stats"]("brannoc", ac=0)
        self.assertEqual(self.camp.read_json("campaign.json")["npcs"]["brannoc"]["ac"], 0)

    def test_omitted_fields_are_kept(self):
        self.tools["set_npc_stats"]("brannoc", cls="Fighter")
        npc = self.camp.read_json("campaign.json")["npcs"]["brannoc"]
        self.assertEqual((npc["ac"], npc["thac0"], npc["hp_max"]), (7, 20, 8))


if __name__ == "__main__":
    unittest.main()
