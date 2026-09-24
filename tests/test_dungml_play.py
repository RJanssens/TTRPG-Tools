"""Unit tests for tools.dungml_play — the fog-of-war bridge.

The HTTP layer is stubbed, so these run without dmap-server. What they pin
down is the behaviour the combat tab depends on: which contract a call goes
to, that an unreachable map service degrades to "not connected" rather than
raising, and that geometry/party state is read back from the render rather
than trusted from cache.

Run: python3 -m unittest tests.test_dungml_play
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import dungml_play as dp


SVG_FOG = (
    '<svg data-party-node="room.entry" xmlns="http://www.w3.org/2000/svg" '
    'viewBox="0 0 36 18" width="576" height="288">'
    '<g class="rooms"><path class="floor" data-room="entry"/></g></svg>'
).encode()

SVG_PLACEHOLDER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="60">'
    '<text x="10" y="35" font-size="12">map not ready</text></svg>'
).encode()


class _Recorder:
    """Stands in for dungml_play._request, recording calls and replaying
    canned responses keyed by (method, path-prefix)."""

    def __init__(self, responses: dict | None = None, fail: bool = False):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.responses = responses or {}
        self.fail = fail

    def __call__(self, method, path, body=None, *, auth=True, raw=False):
        self.calls.append((method, path, body))
        if self.fail:
            raise dp.DungmlUnavailable("stubbed outage")
        for (m, prefix), value in self.responses.items():
            if m == method and path.startswith(prefix):
                return value
        return b"" if raw else {}

    def paths(self, method: str | None = None) -> list[str]:
        return [p for m, p, _ in self.calls if method is None or m == method]

    def body_for(self, method: str, prefix: str) -> dict | None:
        for m, p, b in self.calls:
            if m == method and p.startswith(prefix):
                return b
        return None


class _FakeState:
    """Minimal stand-in for _campaign's state load/save pair."""

    def __init__(self):
        self.state: dict = {}

    def load(self, cfg):
        return self.state

    def save(self, cfg, state):
        self.state = state


class BridgeTestCase(unittest.TestCase):
    """Isolates the module's two side channels: HTTP and campaign state."""

    def setUp(self):
        self.cfg = {"_dir": Path("/nonexistent")}
        self.fake_state = _FakeState()
        self._orig_request = dp._request
        self._orig_load = dp._c.load_state
        self._orig_save = dp._c.save_state
        self._orig_active = dp._c.get_active_name
        dp._c.load_state = self.fake_state.load
        dp._c.save_state = self.fake_state.save
        dp._c.get_active_name = lambda: "test-camp"

    def tearDown(self):
        dp._request = self._orig_request
        dp._c.load_state = self._orig_load
        dp._c.save_state = self._orig_save
        dp._c.get_active_name = self._orig_active

    def stub(self, **kw) -> _Recorder:
        rec = _Recorder(**kw)
        dp._request = rec
        return rec


class LinkStateTests(BridgeTestCase):
    def test_defaults_when_nothing_stored(self):
        link = dp.link_state(self.cfg)
        self.assertEqual(link["source"], "combat")
        self.assertEqual(link["scope"], "fog")
        self.assertIsNone(link["map_id"])

    def test_save_merges_and_persists(self):
        dp.save_link_state(self.cfg, scope="gm", map_id="m1")
        link = dp.link_state(self.cfg)
        self.assertEqual(link["scope"], "gm")
        self.assertEqual(link["map_id"], "m1")
        # untouched keys keep their defaults
        self.assertEqual(link["source"], "combat")

    def test_unknown_keys_are_ignored(self):
        dp.save_link_state(self.cfg, nonsense="x", scope="gm")
        self.assertNotIn("nonsense", dp.link_state(self.cfg))

    def test_bad_scope_falls_back_to_fog(self):
        """A scope the render contract wouldn't accept must not reach it."""
        dp.save_link_state(self.cfg, scope="everything")
        self.assertEqual(dp.link_state(self.cfg)["scope"], "fog")

    def test_stored_garbage_does_not_crash(self):
        self.fake_state.state["dungml"] = "not-a-dict"
        self.assertEqual(dp.link_state(self.cfg)["source"], "combat")


class ContractRoutingTests(BridgeTestCase):
    """Which contract a call lands on is the whole point of the link's
    `source` — a campaign locale must never be driven through the legacy
    per-campaign map, or the two fogs diverge."""

    def test_combat_source_uses_maps_contract(self):
        rec = self.stub(responses={("POST", "/maps/test-camp/tokens"): {"token": "t"},
                                   ("GET", "/maps/test-camp/render"): SVG_FOG})
        dp.render_current(self.cfg)
        self.assertIn("/maps/test-camp/tokens", rec.paths("POST"))
        self.assertTrue(any(p.startswith("/maps/test-camp/render") for p in rec.paths("GET")))

    def test_campaign_source_uses_campaigns_contract(self):
        dp.save_link_state(self.cfg, source="campaign", map_id="m9")
        rec = self.stub(responses={
            ("POST", "/campaigns/test-camp/maps/m9/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m9/render"): SVG_FOG,
        })
        dp.render_current(self.cfg)
        self.assertIn("/campaigns/test-camp/maps/m9/tokens", rec.paths("POST"))

    def test_campaign_source_without_map_id_falls_back(self):
        """Half-set state (source flipped, map never chosen) must not build a
        /campaigns/…/maps//render URL."""
        dp.save_link_state(self.cfg, source="campaign", map_id=None)
        rec = self.stub(responses={("POST", "/maps/test-camp/tokens"): {"token": "t"},
                                   ("GET", "/maps/test-camp/render"): SVG_FOG})
        dp.render_current(self.cfg)
        self.assertTrue(any(p.startswith("/maps/test-camp/") for p in rec.paths("POST")))

    def test_party_and_reveal_follow_the_same_split(self):
        dp.save_link_state(self.cfg, source="campaign", map_id="m9")
        rec = self.stub()
        dp.campaign_set_party("test-camp", "m9", "room.a")
        dp.campaign_reveal("test-camp", "m9", "room.b")
        self.assertEqual(
            rec.paths("POST"),
            ["/campaigns/test-camp/maps/m9/party", "/campaigns/test-camp/maps/m9/reveal"],
        )

    def test_push_source_replaces_not_appends(self):
        """PUT /source, never POST /fragments — ttrpg2 re-emits its whole DSL
        on every token move, so appending would stack duplicate geometry."""
        rec = self.stub()
        dp.push_source("test-camp", 'map "M" {}')
        self.assertEqual(rec.calls[0][0], "PUT")
        self.assertEqual(rec.calls[0][1], "/maps/test-camp/source")
        self.assertNotIn("/maps/test-camp/fragments", rec.paths())

    def test_external_id_is_quoted(self):
        dp._c.get_active_name = lambda: "camp with spaces"
        rec = self.stub()
        dp.reveal(dp.external_id(self.cfg), "room.a")
        self.assertIn("camp%20with%20spaces", rec.paths("POST")[0])


class RenderMetaTests(BridgeTestCase):
    def test_geometry_and_party_come_from_the_svg(self):
        self.stub(responses={("POST", "/maps/test-camp/tokens"): {"token": "t"},
                             ("GET", "/maps/test-camp/render"): SVG_FOG})
        _svg, meta = dp.render_current(self.cfg)
        self.assertEqual((meta["width"], meta["height"]), (36, 18))
        self.assertEqual(meta["party_node"], "room.entry")
        self.assertFalse(meta["placeholder"])

    def test_placeholder_is_flagged(self):
        self.stub(responses={("POST", "/maps/test-camp/tokens"): {"token": "t"},
                             ("GET", "/maps/test-camp/render"): SVG_PLACEHOLDER})
        _svg, meta = dp.render_current(self.cfg)
        self.assertTrue(meta["placeholder"])

    def test_probe_caches_geometry_on_the_link(self):
        self.stub(responses={("POST", "/maps/test-camp/tokens"): {"token": "t"},
                             ("GET", "/maps/test-camp/render"): SVG_FOG})
        link = dp.probe(self.cfg)
        self.assertEqual(link["width"], 36)
        self.assertEqual(link["party_node"], "room.entry")

    def test_probe_does_not_cache_a_placeholder(self):
        """A not-ready map must not leave bogus dimensions behind — the tab
        uses width to decide whether it can lay out tokens at all."""
        self.stub(responses={("POST", "/maps/test-camp/tokens"): {"token": "t"},
                             ("GET", "/maps/test-camp/render"): SVG_PLACEHOLDER})
        link = dp.probe(self.cfg)
        self.assertIsNone(link["width"])


class GracefulDegradationTests(BridgeTestCase):
    """A map service that's down costs the fog view, never the game."""

    def test_probe_survives_an_outage(self):
        self.stub(fail=True)
        link = dp.probe(self.cfg)
        self.assertIsNone(link["width"])

    def test_sync_combat_map_survives_an_outage(self):
        self.stub(fail=True)
        link = dp.sync_combat_map(self.cfg, 'map "M" {}')
        self.assertEqual(link["source"], "combat")

    def test_connection_block_reports_unreachable(self):
        self.stub(fail=True)
        block = dp.connection_block(self.cfg)
        self.assertFalse(block["connected"])
        self.assertFalse(block["reachable"])
        self.assertIn("unreachable", (block["detail"] or "").lower())

    def test_empty_dsl_is_not_pushed(self):
        rec = self.stub()
        dp.sync_combat_map(self.cfg, "   \n  ")
        self.assertEqual(rec.calls, [])

    def test_health_false_on_outage(self):
        self.stub(fail=True)
        self.assertFalse(dp.health())


class ConnectionBlockTests(BridgeTestCase):
    def test_not_connected_until_a_map_is_pushed(self):
        """A session row exists as soon as the contract map is provisioned,
        but there is nothing to draw until some DSL has been pushed."""
        self.stub(responses={
            ("GET", "/health"): {"status": "ok"},
            ("GET", "/maps/test-camp/info"): {"map_id": "m1", "session_id": "s1"},
        })
        block = dp.connection_block(self.cfg)
        self.assertTrue(block["reachable"])
        self.assertFalse(block["connected"])
        self.assertEqual(block["detail"], "no map pushed to dungml yet")

    def test_connected_once_geometry_is_known(self):
        dp.save_link_state(self.cfg, width=36, height=18)
        self.stub(responses={
            ("GET", "/health"): {"status": "ok"},
            ("GET", "/maps/test-camp/info"): {"map_id": "m1", "session_id": "s1"},
        })
        block = dp.connection_block(self.cfg)
        self.assertTrue(block["connected"])
        self.assertEqual(block["width"], 36)

    def test_campaign_locale_is_connected_without_a_push(self):
        """An authored locale is already drawable — nothing needs pushing."""
        dp.save_link_state(self.cfg, source="campaign", map_id="m9", map_name="Upper Halls")
        self.stub(responses={("GET", "/health"): {"status": "ok"}})
        block = dp.connection_block(self.cfg)
        self.assertTrue(block["connected"])
        self.assertEqual(block["map_name"], "Upper Halls")

    def test_block_carries_the_render_url(self):
        self.stub(responses={("GET", "/health"): {"status": "ok"}})
        self.assertEqual(dp.connection_block(self.cfg)["render_url"], "/combat-fog.svg")


class SyncTests(BridgeTestCase):
    def test_sync_pushes_then_probes(self):
        rec = self.stub(responses={
            ("POST", "/maps/test-camp/tokens"): {"token": "t"},
            ("GET", "/maps/test-camp/render"): SVG_FOG,
        })
        dp.sync_combat_map(self.cfg, 'map "M" { grid { bounds 36 x 18 } }')
        self.assertEqual(rec.calls[0][1], "/maps/test-camp/source")
        self.assertEqual(dp.link_state(self.cfg)["width"], 36)

    def test_sync_does_not_steal_a_selected_locale(self):
        """A DM who pointed the tab at an authored locale keeps it, even
        though combat pushes its own map underneath."""
        dp.save_link_state(self.cfg, source="campaign", map_id="m9", map_name="Upper Halls")
        self.stub(responses={
            ("POST", "/campaigns/test-camp/maps/m9/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m9/render"): SVG_FOG,
        })
        dp.sync_combat_map(self.cfg, 'map "M" {}')
        self.assertEqual(dp.link_state(self.cfg)["source"], "campaign")
        self.assertEqual(dp.link_state(self.cfg)["map_id"], "m9")


class ToolTests(BridgeTestCase):
    """The MCP surface, via the same fake-registrar the composites tests use."""

    def setUp(self):
        super().setUp()

        class _FakeMcp:
            def __init__(self): self.registered = {}
            def tool(self, *a, **kw):
                def deco(fn):
                    self.registered[fn.__name__] = fn
                    return fn
                return deco

        self.mcp = _FakeMcp()
        dp.register(self.mcp)
        self.tools = self.mcp.registered
        self._orig_load_campaign = dp._c.load_campaign
        dp._c.load_campaign = lambda *a, **kw: self.cfg

    def tearDown(self):
        dp._c.load_campaign = self._orig_load_campaign
        super().tearDown()

    def test_view_rejects_a_bad_scope(self):
        self.stub()
        out = self.tools["dungml_view"](scope="wat")
        self.assertIn("error", out)
        self.assertEqual(dp.link_state(self.cfg)["scope"], "fog")

    def test_view_sets_gm(self):
        self.stub()
        out = self.tools["dungml_view"](scope="gm")
        self.assertEqual(out["scope"], "gm")

    def test_use_locale_reverts_to_combat(self):
        dp.save_link_state(self.cfg, source="campaign", map_id="m9")
        self.stub()
        out = self.tools["dungml_use_locale"](name_or_id="combat")
        self.assertEqual(out["source"], "combat")
        self.assertIsNone(dp.link_state(self.cfg)["map_id"])

    def test_use_locale_matches_on_name_substring(self):
        self.stub(responses={
            ("GET", "/campaigns/test-camp/maps"): [
                {"id": "m1", "name": "Upper Halls"},
                {"id": "m2", "name": "Deep Cells"},
            ],
            ("POST", "/campaigns/test-camp/maps/m2/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m2/render"): SVG_FOG,
        })
        out = self.tools["dungml_use_locale"](name_or_id="deep")
        self.assertEqual(out["map_id"], "m2")

    def test_use_locale_refuses_an_ambiguous_name(self):
        self.stub(responses={
            ("GET", "/campaigns/test-camp/maps"): [
                {"id": "m1", "name": "Upper Halls"},
                {"id": "m2", "name": "Upper Vault"},
            ],
        })
        out = self.tools["dungml_use_locale"](name_or_id="upper")
        self.assertIn("error", out)
        self.assertEqual(len(out["matches"]), 2)

    def test_use_locale_reports_no_link(self):
        self.stub(responses={("GET", "/campaigns/test-camp/maps"): []})
        out = self.tools["dungml_use_locale"](name_or_id="anything")
        self.assertIn("error", out)

    def test_locales_notes_an_unlinked_campaign(self):
        self.stub(responses={("GET", "/campaigns/test-camp/maps"): []})
        out = self.tools["dungml_locales"]()
        self.assertEqual(out["locales"], [])
        self.assertIn("not linked", out["note"])

    def test_locales_flags_the_current_one(self):
        dp.save_link_state(self.cfg, source="campaign", map_id="m2")
        self.stub(responses={
            ("GET", "/campaigns/test-camp/maps"): [
                {"id": "m1", "name": "Upper Halls"},
                {"id": "m2", "name": "Deep Cells"},
            ],
        })
        out = self.tools["dungml_locales"]()
        current = [loc for loc in out["locales"] if loc["current"]]
        self.assertEqual([c["map_id"] for c in current], ["m2"])

    def test_party_at_warns_when_the_node_did_not_take(self):
        """An unknown node is a graceful no-op server-side, so the tool has to
        notice the marker didn't move rather than report success."""
        self.stub(responses={
            ("POST", "/maps/test-camp/tokens"): {"token": "t"},
            ("GET", "/maps/test-camp/render"): SVG_FOG,  # says room.entry
        })
        out = self.tools["dungml_party_at"](room="room.nowhere")
        self.assertEqual(out["requested"], "room.nowhere")
        self.assertIsNotNone(out["note"])

    def test_party_at_is_clean_when_it_takes(self):
        self.stub(responses={
            ("POST", "/maps/test-camp/tokens"): {"token": "t"},
            ("GET", "/maps/test-camp/render"): SVG_FOG,
        })
        out = self.tools["dungml_party_at"](room="room.entry")
        self.assertIsNone(out["note"])

    def test_party_at_reports_an_outage(self):
        self.stub(fail=True)
        self.assertIn("error", self.tools["dungml_party_at"](room="room.entry"))

    def test_rooms_errors_without_a_map(self):
        self.stub(responses={("GET", "/maps/test-camp/info"): {}})
        self.assertIn("error", self.tools["dungml_rooms"]())

    def test_rooms_lists_nodes(self):
        self.stub(responses={
            ("GET", "/maps/test-camp/info"): {"map_id": "m1", "session_id": "s1"},
            ("GET", "/maps/m1/rooms"): {"rooms": [{"id": "room.entry", "kind": "room"}]},
        })
        out = self.tools["dungml_rooms"]()
        self.assertEqual(out["rooms"][0]["id"], "room.entry")

    def test_status_reports_disconnected_on_outage(self):
        self.stub(fail=True)
        out = self.tools["dungml_status"]()
        self.assertFalse(out["connected"])
        self.assertEqual(out["external_id"], "test-camp")


if __name__ == "__main__":
    unittest.main()


class ActiveMapReportTests(BridgeTestCase):
    """dungml can't know which locale the party is on — ttrpg2 owns that
    choice and has to tell it, or the web GUI has no way to point the GM at
    the live session."""

    def test_probe_reports_the_active_map(self):
        dp.save_link_state(self.cfg, source="campaign", map_id="m9")
        rec = self.stub(responses={
            ("POST", "/campaigns/test-camp/maps/m9/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m9/render"): SVG_FOG,
        })
        dp.probe(self.cfg)
        self.assertEqual(
            rec.body_for("PUT", "/campaigns/test-camp/active"), {"map_id": "m9"}
        )

    def test_combat_mode_reports_no_active_map(self):
        rec = self.stub(responses={
            ("POST", "/maps/test-camp/tokens"): {"token": "t"},
            ("GET", "/maps/test-camp/render"): SVG_FOG,
        })
        dp.probe(self.cfg)
        self.assertNotIn("/campaigns/test-camp/active", rec.paths("PUT"))

    def test_a_placeholder_render_reports_nothing(self):
        """Nothing is in play yet, so there's no live locale to advertise."""
        dp.save_link_state(self.cfg, source="campaign", map_id="m9")
        rec = self.stub(responses={
            ("POST", "/campaigns/test-camp/maps/m9/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m9/render"): SVG_PLACEHOLDER,
        })
        dp.probe(self.cfg)
        self.assertNotIn("/campaigns/test-camp/active", rec.paths("PUT"))

    def test_an_outage_while_reporting_does_not_break_the_probe(self):
        dp.save_link_state(self.cfg, source="campaign", map_id="m9")
        self.stub(responses={
            ("POST", "/campaigns/test-camp/maps/m9/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m9/render"): SVG_FOG,
        })
        orig = dp.set_active_map

        def boom(ext, map_id):
            raise dp.DungmlUnavailable("stubbed outage")

        dp.set_active_map = boom
        try:
            link = dp.probe(self.cfg)
        finally:
            dp.set_active_map = orig
        # Geometry still cached — the report is advisory, not load-bearing.
        self.assertEqual(link["width"], 36)

    def test_use_locale_reports_the_chosen_map(self):
        rec = self.stub(responses={
            ("GET", "/campaigns/test-camp/maps"): [
                {"id": "m1", "name": "Upper Halls"},
                {"id": "m2", "name": "Deep Cells"},
            ],
            ("POST", "/campaigns/test-camp/maps/m2/tokens"): {"token": "t"},
            ("GET", "/campaigns/test-camp/maps/m2/render"): SVG_FOG,
        })

        class _FakeMcp:
            def __init__(self): self.registered = {}
            def tool(self, *a, **kw):
                def deco(fn):
                    self.registered[fn.__name__] = fn
                    return fn
                return deco

        mcp = _FakeMcp()
        dp.register(mcp)
        orig_load = dp._c.load_campaign
        dp._c.load_campaign = lambda *a, **kw: self.cfg
        try:
            mcp.registered["dungml_use_locale"](name_or_id="deep")
        finally:
            dp._c.load_campaign = orig_load
        self.assertEqual(
            rec.body_for("PUT", "/campaigns/test-camp/active"), {"map_id": "m2"}
        )
