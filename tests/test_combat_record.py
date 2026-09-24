"""What end_combat leaves in the event log.

The XP double-award came from a combat event that recorded only party HP:
after a reset nobody could say how many goblins had died.

Run: python3 -m unittest tests.test_combat_record
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._campaign_fixture import TempCampaign
from tools import combat


def _c(name, side, hp, hp_max, conditions=()):
    return {"name": name, "side": side, "hp": hp, "hp_max": hp_max,
            "conditions": list(conditions), "effects": []}


class EndCombatRecordTests(unittest.TestCase):
    def test_event_records_each_enemy_and_its_fate(self):
        self.camp = TempCampaign(self)
        session = {"round": 4, "current_idx": 0, "combatants": [
            _c("Durgan", "party", 6, 10),
            _c("Goblin 1", "enemy", -2, 5),
            _c("Goblin 2", "enemy", 0, 6),
            _c("Goblin 3", "enemy", 4, 4, ["fleeing"]),
            _c("Goblin 4", "enemy", 3, 5),
        ]}
        with mock.patch.object(combat, "_session", session):
            TempCampaign.register(combat)["end_combat"]()
        ev = [e for e in self.camp.events() if e["type"] == "combat"][-1]
        fates = {e["name"]: e["fate"] for e in ev["enemies"]}
        self.assertEqual(fates, {"Goblin 1": "slain", "Goblin 2": "down",
                                 "Goblin 3": "fled", "Goblin 4": "standing"})
        self.assertEqual(ev["defeated"], {"goblin": 2})


if __name__ == "__main__":
    unittest.main()
