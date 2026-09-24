"""/play helpers that read campaign state.

Run: .venv/bin/python -m unittest tests.test_dashboard_play
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _campaign as _c
from tests._campaign_fixture import TempCampaign

try:
    import dashboard
except ImportError:                      # flask missing: run under .venv
    dashboard = None


@unittest.skipIf(dashboard is None, "dashboard deps not installed")
class LatestLocationTests(unittest.TestCase):
    def test_reads_location_visited_from_events_jsonl(self):
        TempCampaign(self, events=[
            {"type": "location_visited", "slug": "wickmere", "name": "Wickmere"},
            {"type": "location_visited", "slug": "stonehell-gate", "name": "Stonehell Gate"},
        ])
        with mock.patch.object(dashboard, "cfg", _c.load_campaign()):
            self.assertEqual(dashboard._latest_location_name(), "Stonehell Gate")

    def test_prefers_position_over_events(self):
        TempCampaign(self, state={"position": {"name": "Wickmere", "label": "39",
                                               "map_name": "Level 1A"}},
                     events=[{"type": "location_visited", "slug": "x", "name": "Old"}])
        with mock.patch.object(dashboard, "cfg", _c.load_campaign()):
            self.assertEqual(dashboard._latest_location_name(), "39 · Level 1A")


if __name__ == "__main__":
    unittest.main()
