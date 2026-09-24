"""Where the party is: one record, state.json[position].

Run: python3 -m unittest tests.test_position
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _campaign as _c
from tests._campaign_fixture import TempCampaign
from tools import position, world


class PositionTests(unittest.TestCase):
    def test_empty_campaign_has_no_position(self):
        TempCampaign(self)
        self.assertEqual(position.current_position(_c.load_campaign()), {})

    def test_falls_back_to_last_location_visited_event(self):
        # A campaign from before position existed.
        TempCampaign(self, events=[
            {"type": "location_visited", "slug": "wickmere", "area": "", "name": "Wickmere"}])
        pos = position.current_position(_c.load_campaign())
        self.assertEqual((pos["location"], pos["name"]), ("wickmere", "Wickmere"))

    def test_set_position_merges_and_stamps_time(self):
        camp = TempCampaign(self, state={"current_hour": 12, "current_minute": 42})
        cfg = _c.load_campaign()
        position.set_position(cfg, location="stonehell-gate", name="The Gate")
        position.set_position(cfg, map_id="m1", node="room.canyon", label="The Canyon")
        pos = camp.read_json("state.json")["position"]
        self.assertEqual(pos["location"], "stonehell-gate")
        self.assertEqual(pos["node"], "room.canyon")
        self.assertEqual(pos["since"], {"day": 1, "time": "12:42"})

    def test_find_location_by_bare_slug_in_an_area(self):
        camp = TempCampaign(self)
        camp.write("locations/wickmere/peacock-and-pike.md", "# The Peacock and Pike\n")
        loc = position.find_location(_c.load_campaign(), "peacock-and-pike")
        self.assertEqual((loc["area"], loc["name"]), ("wickmere", "The Peacock and Pike"))
        self.assertIsNone(position.find_location(_c.load_campaign(), "nowhere"))

    def test_find_location_rejects_path_traversal(self):
        camp = TempCampaign(self)
        camp.write("characters/secret.md", "# Secret\nDM-only notes.\n")
        # locations/ must actually exist for the traversal to be tempting at all.
        camp.write("locations/wickmere/x.md", "# X\n")
        self.assertIsNone(position.find_location(_c.load_campaign(), "../characters/secret"))
        self.assertIsNone(position.find_location(_c.load_campaign(), "/etc/passwd"))

    def test_find_location_area_slug_must_match_actual_area(self):
        camp = TempCampaign(self)
        camp.write("locations/wickmere/peacock-and-pike.md", "# The Peacock and Pike\n")
        self.assertIsNone(
            position.find_location(_c.load_campaign(), "otherarea/peacock-and-pike"))
        # bare slug lookup is unaffected — it still finds it under its real area.
        loc = position.find_location(_c.load_campaign(), "peacock-and-pike")
        self.assertEqual(loc["area"], "wickmere")

    def test_label_prefers_room_then_location(self):
        self.assertEqual(position.position_label(
            {"name": "Gate", "label": "39", "map_name": "Level 1A"}), "39 · Level 1A")
        self.assertEqual(position.position_label({"name": "Wickmere"}), "Wickmere")
        self.assertEqual(position.position_label({}), "")


class CreatingIsNotTravellingTests(unittest.TestCase):
    def test_create_location_does_not_move_the_party(self):
        camp = TempCampaign(self)
        tools = TempCampaign.register(world)
        tools["create_location"]("far-tower", "The Far Tower", "Tall.")
        self.assertFalse([e for e in camp.events() if e["type"] == "location_visited"])
        self.assertEqual(position.current_position(_c.load_campaign()), {})


if __name__ == "__main__":
    unittest.main()
