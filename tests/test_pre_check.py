"""pre_turn_check against a temporary campaign.

Run: python3 -m unittest tests.test_pre_check
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests._campaign_fixture import TempCampaign
from tools import pre_check


class PreTurnCheckTests(unittest.TestCase):
    def check(self, situation="", **camp):
        self.camp = TempCampaign(self, **camp)
        return TempCampaign.register(pre_check)["pre_turn_check"](situation)

    def test_active_effects_come_from_the_combat_session(self):
        self.camp = TempCampaign(self)
        (self.camp.dir / "combat_session.json").write_text(json.dumps({
            "round": 3, "current_idx": 1,
            "combatants": [
                {"name": "Durgan", "effects": [{"name": "bless", "duration": 4, "to_hit": 1}]},
                {"name": "Goblin 1", "effects": []},
            ]}))
        out = TempCampaign.register(pre_check)["pre_turn_check"]("")
        combat = out["session_state"]["combat"]
        self.assertTrue(combat["in_combat"])
        self.assertEqual(combat["round"], 3)
        self.assertEqual(combat["current"], "Goblin 1")
        self.assertEqual(combat["active_effects"],
                         [{"target": "Durgan", "name": "bless", "rounds_left": 4}])

    def test_no_combat_session_means_not_in_combat(self):
        out = self.check()
        self.assertFalse(out["session_state"]["combat"]["in_combat"])

    def test_npc_named_in_situation_counts_as_in_scene(self):
        # Met in an earlier session: no npc_met event in this one.
        out = self.check(
            "Leoric the brigand stops the cart",
            campaign={"npcs": {"leoric": {"label": "Leoric", "disposition": -30}}},
            state={"current_session": 2})
        scene = out["session_state"]["npcs_recently_in_scene"]
        self.assertEqual([n["slug"] for n in scene], ["leoric"])
        self.assertEqual(scene[0]["disposition"], -30)

    def test_reports_position_label_and_perceived_exits(self):
        from unittest import mock
        from tools import dungml_play as dp
        self.camp = TempCampaign(self, state={
            "dungml": {"source": "campaign", "map_id": "m1"},
            "position": {"map_id": "m1", "node": "room.a", "label": "39", "map_name": "L1"}})
        ctx = {"id": "room.a", "perceived": {"exits": [
            {"to_label": "37", "state": "closed", "type": "wooden", "door": "1,1"}]}}
        with mock.patch.object(dp, "campaign_room", return_value=ctx):
            out = TempCampaign.register(pre_check)["pre_turn_check"]("")
        pos = out["session_state"]["position"]
        self.assertEqual(pos["label"], "39 · L1")
        self.assertEqual(pos["exits"], [{"to_label": "37", "state": "closed", "type": "wooden"}])

    def test_unexplored_exit_has_no_to_label_and_no_dm_side_name(self):
        # dungml nulls to_label for an exit whose far side is unexplored and
        # keeps the name only in dm_only.exit_labels; nothing of that reaches
        # pre_turn_check's position.
        from unittest import mock
        from tools import dungml_play as dp
        self.camp = TempCampaign(self, state={
            "dungml": {"source": "campaign", "map_id": "m1"},
            "position": {"map_id": "m1", "node": "room.a", "label": "39", "map_name": "L1"}})
        ctx = {"id": "room.a",
               "perceived": {"exits": [
                   {"door": "1,1", "to": "room.b", "to_label": None, "state": "closed",
                    "type": "wooden", "far_side_explored": False}]},
               "dm_only": {"exit_labels": {"1,1": "Vault of Ghouls"}}}
        with mock.patch.object(dp, "campaign_room", return_value=ctx):
            out = TempCampaign.register(pre_check)["pre_turn_check"]("")
        pos = out["session_state"]["position"]
        self.assertEqual(pos["exits"], [{"to_label": None, "state": "closed", "type": "wooden"}])
        self.assertNotIn("Vault", str(out))

    def test_room_lookup_uses_a_short_timeout(self):
        from unittest import mock
        from tools import dungml_play as dp
        self.camp = TempCampaign(self, state={
            "dungml": {"source": "campaign", "map_id": "m1"},
            "position": {"map_id": "m1", "node": "room.a", "label": "39"}})
        with mock.patch.object(dp, "campaign_room", return_value={}) as cr:
            TempCampaign.register(pre_check)["pre_turn_check"]("")
        self.assertEqual(cr.call_args.kwargs.get("timeout"), 3.0)


if __name__ == "__main__":
    unittest.main()
