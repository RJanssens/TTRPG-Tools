"""Unit tests for the session_primer scene block.

The block exists so the DM never has to reconstruct a scene by reading
campaign files by hand — measured at 5 Bash reads, i.e. 5 model round trips,
on a session-start turn.

Run: python3 -m unittest tests.test_session_primer
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import lore


class TailAndClipTests(unittest.TestCase):
    def test_tail_returns_everything_when_short(self):
        self.assertEqual(lore._tail("short text", 100), "short text")

    def test_tail_keeps_the_end_and_marks_the_cut(self):
        text = "a" * 50 + "\n\n" + "b" * 200
        out = lore._tail(text, 100)
        self.assertTrue(out.startswith("…"))
        self.assertTrue(out.rstrip().endswith("b"))
        self.assertNotIn("a" * 50, out)

    def test_clip_marks_truncation(self):
        self.assertEqual(lore._clip("hello", 100), "hello")
        out = lore._clip("x" * 200, 50)
        self.assertIn("[truncated]", out)
        self.assertLess(len(out), 200)


class LightBlockTests(unittest.TestCase):
    def test_burning_and_guttering(self):
        out = lore._light_block({"light_sources": [
            {"type": "torch", "holder": "Pippa", "minutes_remaining": 40, "lit": True},
            {"type": "lantern", "holder": "Aldric", "minutes_remaining": 3, "lit": True},
        ]})
        self.assertTrue(out["any_lit"])
        statuses = {s["holder"]: s["status"] for s in out["lit"]}
        self.assertEqual(statuses["Pippa"], "burning")
        self.assertEqual(statuses["Aldric"], "guttering")

    def test_spent_sources_are_counted_not_listed(self):
        # A long crawl accumulates dozens of stubs; the DM needs the count.
        sources = [{"type": "torch", "holder": "Aldric",
                    "minutes_remaining": 0, "lit": False} for _ in range(9)]
        out = lore._light_block({"light_sources": sources})
        self.assertFalse(out["any_lit"])
        self.assertEqual(out["burnt_out_count"], 9)
        self.assertEqual(out["lit"], [])

    def test_no_sources_at_all(self):
        out = lore._light_block({})
        self.assertFalse(out["any_lit"])
        self.assertEqual(out["burnt_out_count"], 0)


class SceneBlockTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        (root / "locations" / "mount-ederglow").mkdir(parents=True)
        (root / "locations" / "mount-ederglow.md").write_text(
            "# Mount Ederglow\n\nA burning hill.", encoding="utf-8")
        (root / "locations" / "mount-ederglow" / "bowl.md").write_text(
            "# The Glowing Bowl\n\nGlassed stone underfoot.", encoding="utf-8")
        (root / "adventure_log.md").write_text(
            "### Session 7\n\nThe party climbed the slope.", encoding="utf-8")
        self.cfg = {"_dir": root, "_data_dir": root}

    def test_resolves_the_last_location_visited(self):
        events = [
            {"type": "location_visited", "slug": "brinewick", "area": "", "name": "Brinewick"},
            {"type": "location_visited", "slug": "bowl", "area": "mount-ederglow",
             "name": "The Glowing Bowl", "day": 219, "session": 7},
        ]
        loc = lore._current_location(self.cfg, events)
        self.assertEqual(loc["slug"], "bowl")
        self.assertEqual(loc["area"], "mount-ederglow")
        self.assertIn("Glassed stone", loc["content"])
        # The parent area file comes along: the DM needs the surroundings too.
        self.assertIn("burning hill", loc["area_content"])

    def test_missing_location_file_is_flagged_not_faked(self):
        events = [{"type": "location_visited", "slug": "nowhere", "area": "", "name": "Nowhere"}]
        loc = lore._current_location(self.cfg, events)
        self.assertEqual(loc["content"], "")
        self.assertIn("No location file", loc["note"])

    def test_no_location_events_yields_none(self):
        self.assertIsNone(lore._current_location(self.cfg, [{"type": "combat"}]))

    def test_scene_block_carries_narrative_light_and_consumables(self):
        state = {
            "light_sources": [{"type": "torch", "holder": "Pippa",
                               "minutes_remaining": 30, "lit": True}],
            "characters": {"brenna": {"conditions": ["downed"]},
                           "pippa": {"conditions": []}},
            "consumables": {"pippa": {"torch": 3, "oil": 0}, "aldric": {}},
        }
        scene = lore._scene_block(self.cfg, state, [])
        self.assertIn("climbed the slope", scene["recent_narrative"])
        self.assertTrue(scene["light"]["any_lit"])
        # Only characters with something to report appear.
        self.assertEqual(scene["conditions"], {"brenna": ["downed"]})
        self.assertEqual(scene["consumables"], {"pippa": {"torch": 3}})
        self.assertNotIn("combat", scene)

    def test_scene_block_includes_live_combat(self):
        (self.cfg["_dir"] / "combat_state.json").write_text(json.dumps({
            "active": True, "round": 3,
            "combatants": [{"name": "Goblin 1", "side": "enemy", "hp": 2,
                            "hp_max": 7, "init": 9, "current": True,
                            "conditions": [], "portrait_url": "/images/x.png"}],
        }), encoding="utf-8")
        scene = lore._scene_block(self.cfg, {}, [])
        self.assertEqual(scene["combat"]["round"], 3)
        self.assertEqual(scene["combat"]["combatants"][0]["hp"], 2)
        # Dashboard-only fields stay out of the model's context.
        self.assertNotIn("portrait_url", scene["combat"]["combatants"][0])

    def test_ended_combat_is_omitted(self):
        (self.cfg["_dir"] / "combat_state.json").write_text(
            json.dumps({"active": False, "combatants": []}), encoding="utf-8")
        self.assertNotIn("combat", lore._scene_block(self.cfg, {}, []))


if __name__ == "__main__":
    unittest.main()
