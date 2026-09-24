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

    # ----- fix round 1 -----

    def test_map_without_to_is_an_error_and_touches_nothing(self):
        with mock.patch.object(dp, "campaign_maps") as cm:
            out = self.tools["move_party"](map="quiet halls")
        self.assertIn("error", out)
        cm.assert_not_called()
        link = dp.link_state(_c.load_campaign())
        self.assertEqual((link["source"], link["map_id"], link["map_name"]),
                         ("campaign", "m1", "Level 1A"))

    def test_map_and_to_failure_restores_the_previous_link(self):
        err = dp.DungmlNotFound("404", {"error": "unknown room"})
        with mock.patch.object(dp, "campaign_maps",
                               return_value=[{"id": "m2", "name": "Level 1B: The Quiet Halls"}]), \
             mock.patch.object(dp, "probe", return_value={}), \
             mock.patch.object(dp, "campaign_set_party", side_effect=err):
            out = self.tools["move_party"]("zz", map="quiet")
        self.assertIn("error", out)
        link = dp.link_state(_c.load_campaign())
        self.assertEqual((link["source"], link["map_id"], link["map_name"]),
                         ("campaign", "m1", "Level 1A"))

    def test_location_only_move_clears_the_room_half(self):
        self.camp.write("locations/wickmere.md", "# Wickmere\n\nA market town.")
        with mock.patch.object(dp, "campaign_set_party",
                               return_value={"ok": True, "party_location": "room.cave_2", "room": ROOM}), \
             mock.patch.object(dp, "probe", return_value={}):
            self.tools["move_party"]("37")
        self.tools["move_party"](location="wickmere")
        pos = self.camp.read_json("state.json")["position"]
        self.assertIsNone(pos["node"])
        moves = [e for e in self.camp.events() if e["type"] == "location_visited"]
        self.assertEqual(moves[-1]["name"], "Wickmere")

    def test_missing_node_in_response_is_an_error_and_moves_nothing(self):
        with mock.patch.object(dp, "campaign_set_party", return_value={"ok": True}):
            out = self.tools["move_party"]("37")
        self.assertIn("error", out)
        self.assertNotIn("position", self.camp.read_json("state.json"))
        self.assertFalse([e for e in self.camp.events() if e["type"] == "location_visited"])


class MovePartyNoNodeWithLocationTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self, state=dict(LINKED, position={
            "map_id": "m1", "map_name": "Level 1A", "node": "room.cave_2", "label": "37"}))
        self.tools = TempCampaign.register(rooms)

    def test_no_node_with_location_moves_location_clears_map_and_warns(self):
        self.camp.write("locations/wickmere.md", "# Wickmere\n\nA market town.")
        with mock.patch.object(dp, "campaign_set_party", return_value={"ok": True}):
            out = self.tools["move_party"]("37", location="wickmere")
        self.assertNotIn("error", out)
        self.assertIn("warning", out)
        pos = self.camp.read_json("state.json")["position"]
        self.assertEqual(pos["location"], "wickmere")
        self.assertIsNone(pos["node"])
        self.assertIsNone(pos["map_id"])


class MovePartyCombatMapTests(unittest.TestCase):
    """The un-linked (combat-map) branch of `to` — no authored locale."""

    def setUp(self):
        # A combat map has been pushed and probed: the link knows its size.
        self.camp = TempCampaign(self, state={"dungml": {"source": "combat", "width": 36,
                                                         "height": 18}})
        self.tools = TempCampaign.register(rooms)

    def test_missing_node_is_an_error_and_moves_nothing(self):
        with mock.patch.object(dp, "map_info", return_value={"map_id": "cm1"}) as mi, \
             mock.patch.object(dp, "set_party", return_value={"ok": True}) as sp:
            out = self.tools["move_party"]("1")
        self.assertIn("error", out)
        mi.assert_called_once()
        sp.assert_called_once()
        self.assertNotIn("position", self.camp.read_json("state.json"))

    def test_map_info_failure_reports_unreachable_without_calling_set_party(self):
        with mock.patch.object(dp, "map_info", side_effect=dp.DungmlUnavailable("down")), \
             mock.patch.object(dp, "set_party") as sp:
            out = self.tools["move_party"]("1")
        self.assertIn("error", out)
        sp.assert_not_called()


class MovePartyNoMapTests(unittest.TestCase):
    """No authored locale selected and no combat map ever pushed: dungml has
    nothing to move a marker on, so it must not be called at all."""

    def setUp(self):
        self.camp = TempCampaign(self)
        self.tools = TempCampaign.register(rooms)
        patcher = mock.patch.object(dp, "_request",
                                    side_effect=AssertionError("no HTTP call expected"))
        self.req = patcher.start()
        self.addCleanup(patcher.stop)

    def test_to_naming_a_location_moves_the_location(self):
        self.camp.write("locations/wickmere.md", "# Wickmere\n\nA market town.")
        out = self.tools["move_party"]("wickmere")
        self.req.assert_not_called()
        self.assertNotIn("error", out)
        pos = self.camp.read_json("state.json")["position"]
        self.assertEqual(pos["location"], "wickmere")
        self.assertIsNone(pos.get("node"))
        self.assertIn("market town", out["location_brief"])

    def test_to_naming_nothing_is_an_error_and_moves_nothing(self):
        out = self.tools["move_party"]("37")
        self.req.assert_not_called()
        self.assertIn("no map selected", out["error"])
        self.assertNotIn("position", self.camp.read_json("state.json"))
        self.assertFalse([e for e in self.camp.events() if e["type"] == "location_visited"])

    def test_to_with_location_moves_the_location_and_warns(self):
        self.camp.write("locations/wickmere.md", "# Wickmere\n\nA market town.")
        out = self.tools["move_party"]("37", location="wickmere")
        self.req.assert_not_called()
        self.assertIn("warning", out)
        pos = self.camp.read_json("state.json")["position"]
        self.assertEqual(pos["location"], "wickmere")
        self.assertIsNone(pos.get("node"))


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

    # ----- fix round 1 -----

    def test_door_with_comma_in_a_label_is_treated_as_a_room(self):
        with mock.patch.object(dp, "campaign_door",
                               return_value={"ok": True, "door": "1,2", "state": "open",
                                             "discovered": True}) as cd:
            self.tools["door"]("Hall, East", state="open")
        cd.assert_called_once_with("test-campaign", "m1", door=None,
                                   between=["room.cave_2", "Hall, East"],
                                   discovered=True, state="open")

    def test_note_room_without_query_errors_off_an_authored_locale(self):
        self.camp.write_json("state.json", {"dungml": {"source": "combat"},
                                            "position": {"map_id": "m1", "node": "room.cave_2",
                                                         "label": "37"}})
        out = self.tools["note_room"]("Cleared.")
        self.assertIn("error", out)

    def test_note_room_without_query_errors_if_locale_does_not_match_position(self):
        self.camp.write_json("state.json", {"dungml": {"source": "campaign", "map_id": "m9",
                                                        "map_name": "Other"},
                                            "position": {"map_id": "m1", "map_name": "Level 1A",
                                                         "node": "room.cave_2", "label": "37"}})
        out = self.tools["note_room"]("Cleared.")
        self.assertIn("error", out)

    # ----- final review -----

    def _stale(self):
        self.camp.write_json("state.json", {"dungml": {"source": "campaign", "map_id": "m9",
                                                        "map_name": "Other"},
                                            "position": {"map_id": "m1", "map_name": "Level 1A",
                                                         "node": "room.cave_2", "label": "37"}})

    def test_room_without_query_errors_if_locale_does_not_match_position(self):
        self._stale()
        with mock.patch.object(dp, "campaign_room") as cr:
            out = self.tools["room"]()
        cr.assert_not_called()
        self.assertIn("another map", out["error"])

    def test_room_with_query_still_works_on_another_locale(self):
        self._stale()
        with mock.patch.object(dp, "campaign_room", return_value=ROOM) as cr:
            self.tools["room"]("37")
        cr.assert_called_once_with("test-campaign", "m9", "37")

    def test_door_by_neighbour_errors_if_locale_does_not_match_position(self):
        self._stale()
        with mock.patch.object(dp, "campaign_door") as cd:
            out = self.tools["door"]("39", state="open")
        cd.assert_not_called()
        self.assertIn("another map", out["error"])

    def test_bare_door_call_is_an_error(self):
        with mock.patch.object(dp, "campaign_door") as cd:
            out = self.tools["door"]("39")
        cd.assert_not_called()
        self.assertIn("error", out)

    def test_found_without_state_marks_discovered(self):
        with mock.patch.object(dp, "campaign_door", return_value={"ok": True}) as cd:
            self.tools["door"]("39", found=True)
        cd.assert_called_once_with("test-campaign", "m1", door=None,
                                   between=["room.cave_2", "39"], discovered=True, state=None)


if __name__ == "__main__":
    unittest.main()
