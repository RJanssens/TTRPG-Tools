"""A throwaway campaign on disk, with every tool pointed at it.

Tests that call real tools (session_primer, pre_turn_check, move_party …)
need a campaign.json, a state.json and an events log — and must never touch
the real `campaigns/` tree. TempCampaign repoints _campaign at a temp dir for
the duration of a test and clears its caches both ways.

    class MyTests(unittest.TestCase):
        def setUp(self):
            self.camp = TempCampaign(self, campaign={...}, state={...})
            self.tools = self.camp.register(lore)
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _campaign as _c

SLUG = "test-campaign"


class _Registry:
    """Stand-in for FastMCP: collects the functions register() decorates."""

    def __init__(self):
        self.tools: dict = {}

    def tool(self, *a, **kw):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco

    def resource(self, *a, **kw):
        return lambda fn: fn

    def prompt(self, *a, **kw):
        return lambda fn: fn


class TempCampaign:
    def __init__(self, test, campaign: dict | None = None,
                 state: dict | None = None, events: list | None = None):
        self._tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.campaigns = root / "campaigns"
        self.dir = self.campaigns / SLUG
        self.dir.mkdir(parents=True)
        (root / ".active").write_text(SLUG)

        for target, value in (("CAMPAIGNS_DIR", self.campaigns),
                              ("ACTIVE_FILE", root / ".active")):
            p = mock.patch.object(_c, target, value)
            p.start()
            test.addCleanup(p.stop)
        _c._invalidate()
        test.addCleanup(_c._invalidate)

        cfg = {"name": "Test", "characters": {}}
        cfg.update(campaign or {})
        self.write_json("campaign.json", cfg)
        st = {"current_day": 1, "current_hour": 8, "current_minute": 0,
              "current_session": 1, "characters": {}}
        st.update(state or {})
        self.write_json("state.json", st)
        if events:
            (self.dir / "events.jsonl").write_text(
                "".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")

    def write_json(self, name: str, data) -> None:
        (self.dir / name).write_text(json.dumps(data), encoding="utf-8")
        _c._invalidate()

    def write(self, rel: str, text: str) -> Path:
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def read_json(self, name: str):
        return json.loads((self.dir / name).read_text(encoding="utf-8"))

    def events(self) -> list:
        return _c.load_events(_c.load_campaign())

    @staticmethod
    def register(*modules) -> dict:
        reg = _Registry()
        for m in modules:
            m.register(reg)
        return reg.tools
