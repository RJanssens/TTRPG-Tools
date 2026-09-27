"""The "did you know" lore-fact store: CRUD, the MCP-facing campaign helper,
and the dashboard endpoints /play pulls its rotation from.

Run: python3 -m unittest tests.test_lore_facts
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import lore_facts as lf

try:
    import dashboard
except ImportError:                      # flask missing: run under .venv
    dashboard = None


class _TempDb(unittest.TestCase):
    """Every test gets its own empty DB, seeded on first connect."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for patch in (mock.patch.object(lf, "LORE_DB", Path(tmp.name) / "facts.db"),
                      mock.patch.object(lf, "_ensured", False)):
            patch.start()
            self.addCleanup(patch.stop)


class StoreTests(_TempDb):
    def test_empty_db_is_seeded(self):
        self.assertEqual(len(lf.list_facts()), len(lf.SEED))

    def test_create_update_delete(self):
        fid = lf.create_fact("Owlbears hug.", category="monster")["id"]
        self.assertEqual(lf.update_fact(fid, enabled=False), {"ok": True, "id": fid})
        self.assertFalse([f for f in lf.list_facts(enabled=True) if f["id"] == fid])
        self.assertEqual(lf.delete_fact(fid), {"ok": True, "id": fid})
        self.assertIn("error", lf.delete_fact(fid))

    def test_blank_text_is_refused(self):
        self.assertIn("error", lf.create_fact("   "))
        fid = lf.create_fact("x")["id"]
        self.assertIn("error", lf.update_fact(fid, text=" "))

    def test_random_draws_only_enabled_and_clamps(self):
        fid = lf.create_fact("hidden", category="zz")["id"]
        lf.update_fact(fid, enabled=0)
        self.assertEqual(lf.random_facts(n=5, category="zz"), [])
        self.assertLessEqual(len(lf.random_facts(n=10_000)), 100)
        self.assertEqual(len(lf.random_facts(n="junk")), 1)

    def test_campaign_filter(self):
        lf.create_fact("Grusk fell in the well.", category="campaign", campaign="Stonehell")
        rows = lf.list_facts(campaign="Stonehell")
        self.assertEqual([r["text"] for r in rows], ["Grusk fell in the well."])


@unittest.skipIf(dashboard is None, "dashboard deps not installed")
class EndpointTests(_TempDb):
    def setUp(self):
        super().setUp()
        self.client = dashboard.app.test_client()

    def test_random_feeds_the_play_rotation(self):
        r = self.client.get("/api/facts/random?n=3")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["count"], 3)

    def test_crud_round_trip(self):
        r = self.client.post("/api/facts", json={"text": "Beholders float."})
        self.assertEqual(r.status_code, 201)
        fid = r.get_json()["id"]
        self.assertEqual(self.client.patch(f"/api/facts/{fid}",
                                           json={"enabled": 0}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/facts/{fid}").status_code, 200)
        self.assertEqual(self.client.delete(f"/api/facts/{fid}").status_code, 404)
        self.assertEqual(self.client.post("/api/facts", json={}).status_code, 400)

    def test_reference_page_lists_facts(self):
        r = self.client.get("/reference/facts")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Lore Facts", r.get_data(as_text=True))
        self.assertIn("/reference/facts",
                      self.client.get("/reference").get_data(as_text=True))

    def test_play_page_carries_the_rotation(self):
        page = self.client.get("/play").get_data(as_text=True)
        self.assertIn("/api/facts/random", page)
        self.assertIn("renderNextFact", page)


if __name__ == "__main__":
    unittest.main()
