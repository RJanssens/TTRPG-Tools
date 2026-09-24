"""move_party and friends: one move updates both systems.

Run: python3 -m unittest tests.test_rooms
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _campaign as _c
from tests._campaign_fixture import TempCampaign
from tools import dungml_play as dp
from tools import rooms

ROOM = {"id": "room.cave_2", "label": "37", "kind": "room", "party_here": True,
        "perceived": {"description": "A damp cave.", "exits": []}, "dm_only": {"notes": "x"}}
LINKED = {"dungml": {"source": "campaign", "map_id": "m1", "map_name": "Level 1A"}}


class MovePartyTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self, state=dict(LINKED))
        self.tools = TempCampaign.register(rooms)

    def test_move_to_room_updates_position_logs_once_and_returns_room(self):
        with mock.patch.object(dp, "campaign_set_party",
                               return_value={"ok": True, "party_location": "room.cave_2", "room": ROOM}) as sp, \
             mock.patch.object(dp, "probe", return_value={}):
            out = self.tools["move_party"]("37")
        sp.assert_called_once_with("test-campaign", "m1", "37")
        self.assertEqual(out["room"]["perceived"]["description"], "A damp cave.")
        pos = self.camp.read_json("state.json")["position"]
        self.assertEqual((pos["node"], pos["label"], pos["map_name"]),
                         ("room.cave_2", "37", "Level 1A"))
        moves = [e for e in self.camp.events() if e["type"] == "location_visited"]
        self.assertEqual(len(moves), 1)
        self.assertEqual(moves[0]["node"], "room.cave_2")

    def test_unknown_room_moves_nothing_and_offers_candidates(self):
        err = dp.DungmlNotFound("404", {"error": "unknown room",
                                        "candidates": [{"id": "room.cpt_1", "label": "Cpt"}]})
        with mock.patch.object(dp, "campaign_set_party", side_effect=err):
            out = self.tools["move_party"]("Cpt")
        self.assertEqual(out["candidates"][0]["id"], "room.cpt_1")
        self.assertNotIn("position", self.camp.read_json("state.json"))
        self.assertFalse([e for e in self.camp.events() if e["type"] == "location_visited"])

    def test_map_service_down_still_moves_the_location_half_and_warns(self):
        self.camp.write("locations/stonehell-gate.md", "# The Gate\n\nA canyon mouth.")
        with mock.patch.object(dp, "campaign_set_party",
                               side_effect=dp.DungmlUnavailable("down")):
            out = self.tools["move_party"]("canyon", location="stonehell-gate")
        self.assertIn("warning", out)
        pos = self.camp.read_json("state.json")["position"]
        self.assertEqual(pos["location"], "stonehell-gate")
        self.assertEqual(len([e for e in self.camp.events() if e["type"] == "location_visited"]), 1)

    def test_location_only_move_needs_no_map(self):
        self.camp.write("locations/wickmere.md", "# Wickmere\n\nA market town.")
        out = self.tools["move_party"](location="wickmere")
        self.assertEqual(out["position"]["name"], "Wickmere")
        self.assertIn("market town", out["location_brief"])

    def test_unknown_location_is_an_error(self):
        out = self.tools["move_party"](location="nowhere")
        self.assertIn("error", out)
        self.assertNotIn("position", self.camp.read_json("state.json"))

    def test_map_argument_switches_locale_first(self):
        with mock.patch.object(dp, "campaign_maps",
                               return_value=[{"id": "m2", "name": "Level 1B: The Quiet Halls"}]), \
             mock.patch.object(dp, "probe", return_value={}), \
             mock.patch.object(dp, "campaign_set_party",
                               return_value={"ok": True, "party_location": "room.x",
                                             "room": dict(ROOM, id="room.x", label="1")}) as sp:
            out = self.tools["move_party"]("1", map="quiet halls")
        sp.assert_called_once_with("test-campaign", "m2", "1")
        self.assertEqual(out["position"]["map_name"], "Level 1B: The Quiet Halls")

    def test_move_returns_that_rooms_notes(self):
        rooms.add_note(_c.load_campaign(), "m1", "room.cave_2", "Horse tied here.")
        with mock.patch.object(dp, "campaign_set_party",
                               return_value={"ok": True, "party_location": "room.cave_2", "room": ROOM}), \
             mock.patch.object(dp, "probe", return_value={}):
            out = self.tools["move_party"]("37")
        self.assertEqual([n["text"] for n in out["room_notes"]], ["Horse tied here."])


class RoomAndDoorTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self, state=dict(LINKED, position={
            "map_id": "m1", "map_name": "Level 1A", "node": "room.cave_2", "label": "37"}))
        self.tools = TempCampaign.register(rooms)

    def test_room_defaults_to_the_current_room(self):
        with mock.patch.object(dp, "campaign_room", return_value=ROOM) as cr:
            out = self.tools["room"]()
        cr.assert_called_once_with("test-campaign", "m1", "room.cave_2")
        self.assertEqual(out["room"]["label"], "37")

    def test_note_room_round_trips(self):
        self.tools["note_room"]("Cleared — 4 troglodytes dead.")
        notes = rooms.notes_for(_c.load_campaign(), "m1", "room.cave_2")
        self.assertEqual(notes[0]["text"], "Cleared — 4 troglodytes dead.")

    def test_door_by_neighbour_resolves_between_current_room_and_it(self):
        with mock.patch.object(dp, "campaign_door",
                               return_value={"ok": True, "door": "3,4", "state": "open",
                                             "discovered": True}) as cd:
            out = self.tools["door"]("39", state="open")
        cd.assert_called_once_with("test-campaign", "m1", door=None,
                                   between=["room.cave_2", "39"], discovered=True, state="open")
        self.assertEqual(out["state"], "open")

    def test_door_by_key_passes_the_key(self):
        with mock.patch.object(dp, "campaign_door",
                               return_value={"ok": True, "door": "3,4", "state": "closed",
                                             "discovered": True}) as cd:
            self.tools["door"]("3,4", found=True)
        cd.assert_called_once_with("test-campaign", "m1", door="3,4", between=None,
                                   discovered=True, state=None)

    def test_room_off_an_authored_locale_says_so(self):
        self.camp.write_json("state.json", {"dungml": {"source": "combat"}})
        out = self.tools["room"]()
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
