"""Unit tests for tools.dm_session.

Run: python3 -m unittest tests.test_dm_session
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import dm_session


class DmSessionTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig = dm_session._SESSION_FILE
        dm_session._SESSION_FILE = Path(self._tmp.name) / ".dm_session"

    def tearDown(self):
        dm_session._SESSION_FILE = self._orig

    def test_session_id_initially_none(self):
        self.assertIsNone(dm_session.session_id())

    def test_init_event_persists_session_id(self):
        evt = dm_session._process_event_line(json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "abc-123",
            "tools": [],
        }))
        self.assertEqual(evt["type"], "system")
        self.assertEqual(dm_session.session_id(), "abc-123")

    def test_resume_round_trip(self):
        # First turn: no prior session → no --resume.
        args = dm_session._build_args("hello", dm_session.session_id())
        self.assertNotIn("--resume", args)

        # Simulate the CLI's init event landing.
        dm_session._process_event_line(json.dumps({
            "type": "system", "subtype": "init", "session_id": "sess-xyz",
        }))

        # Second turn: persisted id flows back into args.
        args2 = dm_session._build_args("again", dm_session.session_id())
        self.assertIn("--resume", args2)
        self.assertEqual(args2[args2.index("--resume") + 1], "sess-xyz")

    def test_reset_clears(self):
        dm_session._save_session_id("to-be-removed")
        self.assertEqual(dm_session.session_id(), "to-be-removed")
        dm_session.reset_session()
        self.assertIsNone(dm_session.session_id())
        # Idempotent — second call must not raise.
        dm_session.reset_session()

    def test_blank_line_returns_none(self):
        self.assertIsNone(dm_session._process_event_line(""))
        self.assertIsNone(dm_session._process_event_line("   \n"))

    def test_malformed_json_yields_raw(self):
        evt = dm_session._process_event_line("not json {")
        self.assertEqual(evt["type"], "raw")
        self.assertIn("not json", evt["line"])

    def test_system_event_without_session_id_is_passed_through(self):
        evt = dm_session._process_event_line(json.dumps({
            "type": "system", "subtype": "shutdown",
        }))
        self.assertEqual(evt["subtype"], "shutdown")
        self.assertIsNone(dm_session.session_id())

    def test_build_args_carries_message_and_flags(self):
        args = dm_session._build_args("the party rests", None)
        self.assertEqual(args[0], dm_session._CLAUDE_BIN)
        self.assertIn("-p", args)
        self.assertEqual(args[args.index("-p") + 1], "the party rests")
        self.assertIn("--output-format", args)
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", args)
        self.assertIn("--allowedTools", args)


if __name__ == "__main__":
    unittest.main()
