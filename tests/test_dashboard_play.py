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


import json as _json
import shutil as _shutil
import subprocess as _subprocess

_NODE = _shutil.which("node")

_REDACT_CASES = r"""
const out = {};
const ctx = {room: {id: 'room.a', perceived: {exits: [{door: '1,1', to_label: null}]},
                    dm_only: {notes: 'Ghouls sleep under the floor.', exit_labels: {'1,1': 'Vault'}}},
             room_notes: []};
out.move = JSON.parse(redactToolResultText('move_party', JSON.stringify(ctx)));
out.nested = JSON.parse(redactToolResultText('batch', JSON.stringify(
  {results: [{rooms: [{id: 'r', exits: [], secret_exits: [{door: '2,2'}]}]},
             {hidden_annotations: [1], secret_map_exits: [2], keep: 3}]})));
out.rooms = JSON.parse(redactToolResultText('dungml_rooms', JSON.stringify(
  {map_id: 'm', rooms: [{id: 'room.a', hidden: false}, {id: 'room.secret', hidden: true}]})));
out.bodyless = ['dm_secrets', 'dm_note', 'module_lookup', 'get_location']
  .map(n => [isBodylessTool(n), redactToolResultText(n, '{"text": "the innkeeper is a spy"}')]);
out.plain = redactToolResultText('roll', 'not json at all');
out.leaky = redactToolResultText('room', 'garbled dm_only: Ghouls');
out.scalar = redactToolResultText('roll', '17');
console.log(JSON.stringify(out));
"""


@unittest.skipIf(dashboard is None or _NODE is None, "dashboard deps or node missing")
class PlayTranscriptRedactionTests(unittest.TestCase):
    """The /play transcript's tool rows never show DM-only material. The
    redaction is inline JS in _PLAY_HTML; this runs that exact block in node."""

    def run_js(self):
        html = dashboard._PLAY_HTML
        begin, end = "// ---- redact:begin ----", "// ---- redact:end ----"
        self.assertIn(begin, html)
        block = html[html.index(begin):html.index(end)]
        proc = _subprocess.run([_NODE, "-e", block + _REDACT_CASES],
                               capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return _json.loads(proc.stdout)

    def test_dm_only_keys_are_dropped_and_marked(self):
        out = self.run_js()
        room = out["move"]["room"]
        self.assertEqual(room["dm_only"], "[hidden]")
        self.assertNotIn("Ghouls", _json.dumps(out["move"]))
        self.assertNotIn("Vault", _json.dumps(out["move"]))
        self.assertEqual(room["perceived"]["exits"][0]["door"], "1,1")

    def test_nested_secret_keys_are_dropped(self):
        out = self.run_js()
        r0 = out["nested"]["results"][0]["rooms"][0]
        self.assertNotIn("secret_exits", r0)
        self.assertEqual(r0["dm_only"], "[hidden]")
        r1 = out["nested"]["results"][1]
        self.assertEqual(r1, {"keep": 3, "dm_only": "[hidden]"})

    def test_hidden_rooms_are_dropped_from_dungml_rooms(self):
        out = self.run_js()
        self.assertEqual([r["id"] for r in out["rooms"]["rooms"]], ["room.a"])
        self.assertEqual(out["rooms"]["dm_only"], "[hidden]")

    def test_dm_side_tools_show_no_body(self):
        out = self.run_js()
        for is_bodyless, text in out["bodyless"]:
            self.assertTrue(is_bodyless)
            self.assertNotIn("spy", text)

    def test_non_json_passes_unless_it_names_a_dm_key(self):
        out = self.run_js()
        self.assertEqual(out["plain"], "not json at all")
        self.assertNotIn("Ghouls", out["leaky"])
        self.assertEqual(out["scalar"], "17")
