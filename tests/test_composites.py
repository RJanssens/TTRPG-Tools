"""Unit tests for the composite tools and the monster stat-line parsers.

Run: python3 -m unittest tests.test_composites
"""

from __future__ import annotations

import asyncio
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import combat
from tools import composites


class MonsterStatParserTests(unittest.TestCase):
    """The MM stores prose, not numbers; setup_combat needs integers."""

    def test_parse_ac_forms(self):
        self.assertEqual(combat._parse_ac("5"), 5)
        self.assertEqual(combat._parse_ac("-2"), -2)
        self.assertEqual(combat._parse_ac("5 (10)"), 5)      # AC with shield/without
        self.assertEqual(combat._parse_ac("0 (base)"), 0)
        self.assertEqual(combat._parse_ac("Varies"), 10)     # falls back
        self.assertEqual(combat._parse_ac("Varies", default=7), 7)

    def test_parse_morale_takes_low_end_of_range(self):
        self.assertEqual(combat._parse_morale("Elite (13-14)"), 13)
        self.assertEqual(combat._parse_morale("Fearless (20)"), 20)
        self.assertEqual(combat._parse_morale("Steady (11-12)"), 11)
        self.assertEqual(combat._parse_morale("11"), 11)
        self.assertEqual(combat._parse_morale("Unsteady"), 10)

    def test_parse_hd_plain_and_signed(self):
        self.assertEqual(combat._parse_hd("3")["dice"], 3)
        hd = combat._parse_hd("4+4")
        self.assertEqual((hd["dice"], hd["mod"]), (4, 4))
        hd = combat._parse_hd("1-1")
        self.assertEqual((hd["dice"], hd["mod"]), (1, -1))

    def test_parse_hd_range_is_not_a_subtraction(self):
        # '5-6' in the MM is an HD range; only the small forms mean "minus".
        hd = combat._parse_hd("5-6")
        self.assertEqual((hd["dice"], hd["mod"]), (5, 0))

    def test_parse_hd_half_rolls_d4(self):
        hd = combat._parse_hd("½")
        self.assertEqual((hd["dice"], hd["sides"]), (1, 4))

    def test_parse_hd_literal_hit_points(self):
        self.assertEqual(combat._parse_hd("1 hp")["fixed"], 1)
        self.assertEqual(combat._parse_hd("1 hit point")["fixed"], 1)
        fixed = combat._parse_hd("1-6 hp")["fixed"]
        self.assertIn(fixed, range(1, 7))

    def test_parse_hd_unusable_lines_report_no_dice(self):
        for raw in ("Varies", "Nil", "See below", "N/A", ""):
            hd = combat._parse_hd(raw)
            self.assertEqual(hd["dice"], 0, raw)
            self.assertIsNone(hd["fixed"], raw)
            self.assertIsNone(combat._roll_monster_hp(hd), raw)

    def test_parse_hd_base_suffix(self):
        self.assertEqual(combat._parse_hd("12 (base)")["dice"], 12)

    def test_roll_monster_hp_respects_dice_and_mod(self):
        random.seed(1)
        for _ in range(50):
            hp = combat._roll_monster_hp({"dice": 4, "sides": 8, "mod": 4, "fixed": None})
            self.assertGreaterEqual(hp, 8)   # 4x1 + 4
            self.assertLessEqual(hp, 36)     # 4x8 + 4

    def test_roll_monster_hp_floors_at_one(self):
        # '1-1' can roll a 1 on the die, giving 0 before the floor.
        random.seed(3)
        for _ in range(50):
            hp = combat._roll_monster_hp({"dice": 1, "sides": 8, "mod": -1, "fixed": None})
            self.assertGreaterEqual(hp, 1)

    def test_parse_damage_dice_notation_passthrough(self):
        self.assertEqual(combat._parse_damage("1d8"), ("1d8", True))
        self.assertEqual(combat._parse_damage("2d6+1"), ("2d6+1", True))

    def test_parse_damage_ranges_preserve_the_spread(self):
        self.assertEqual(combat._parse_damage("1-6"), ("1d6", True))
        # 2-8 must span 2..8, not 2..16.
        self.assertEqual(combat._parse_damage("2-8"), ("1d7+1", True))
        self.assertEqual(combat._parse_damage("3-12"), ("1d10+2", True))

    def test_parse_damage_trailing_prose(self):
        # 'Ogre: 1-10 or by weapon' — the numbers come first.
        self.assertEqual(combat._parse_damage("1-10 or by weapon"), ("1d10", True))
        self.assertEqual(combat._parse_damage("1-6 (by weapon)"), ("1d6", True))

    def test_parse_damage_multi_attack_takes_the_first(self):
        self.assertEqual(combat._parse_damage("5-8/5-8/5-12"), ("1d4+4", True))

    def test_parse_damage_flags_unparseable_lines(self):
        for raw in ("By weapon", "Nil", "Special", "See below", ""):
            notation, ok = combat._parse_damage(raw)
            self.assertFalse(ok, raw)
            self.assertEqual(notation, "1d6", raw)


class _FakeTool:
    def __init__(self, fn):
        self.fn = fn

    async def run(self, arguments, context=None, convert_result=False):
        return self.fn(**arguments)


class _FakeToolManager:
    """Minimal stand-in for FastMCP's ToolManager: enough for batch()."""

    def __init__(self, tools):
        self._tools = tools
        self.calls: list[tuple] = []

    def get_tool(self, name):
        return self._tools.get(name)

    async def call_tool(self, name, arguments, context=None, convert_result=False):
        self.calls.append((name, dict(arguments)))
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(name)
        return await tool.run(arguments)


class _FakeMcp:
    def __init__(self, tools):
        self._tool_manager = _FakeToolManager(tools)
        self.registered: dict = {}

    def tool(self, *a, **kw):
        def deco(fn):
            self.registered[fn.__name__] = fn
            return fn
        return deco


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.mcp = _FakeMcp({
            "ok":      _FakeTool(lambda **kw: {"echo": kw}),
            "fails":   _FakeTool(lambda **kw: {"error": "nope"}),
            "raises":  _FakeTool(lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))),
        })
        composites.register(self.mcp)
        self.batch = self.mcp.registered["batch"]

    def run_batch(self, **kw):
        return asyncio.run(self.batch(**kw))

    def test_runs_steps_in_order(self):
        out = self.run_batch(steps=[
            {"tool": "ok", "args": {"n": 1}},
            {"tool": "ok", "args": {"n": 2}},
            {"tool": "ok", "args": {"n": 3}},
        ])
        self.assertEqual(out["ran"], 3)
        self.assertFalse(out["stopped_early"])
        self.assertEqual([c[1]["n"] for c in self.mcp._tool_manager.calls], [1, 2, 3])

    def test_step_args_may_include_name(self):
        # apply_combat_damage, apply_effect, create_location … all take `name`;
        # it must reach the tool, not collide with the dispatcher's own arg.
        out = self.run_batch(steps=[{"tool": "ok", "args": {"name": "Ruk", "amount": 3}}])
        self.assertEqual(out["results"][0].get("result"),
                         {"echo": {"name": "Ruk", "amount": 3}})

    def test_empty_and_oversized_are_rejected(self):
        self.assertIn("error", self.run_batch(steps=[]))
        too_many = [{"tool": "ok", "args": {}}] * (composites._MAX_STEPS + 1)
        self.assertIn("error", self.run_batch(steps=too_many))

    def test_stops_on_error_result_by_default(self):
        out = self.run_batch(steps=[
            {"tool": "ok", "args": {}},
            {"tool": "fails", "args": {}},
            {"tool": "ok", "args": {"never": True}},
        ])
        self.assertTrue(out["stopped_early"])
        self.assertEqual(out["ran"], 2)
        self.assertEqual(len(self.mcp._tool_manager.calls), 2)

    def test_continues_past_errors_when_asked(self):
        out = self.run_batch(stop_on_error=False, steps=[
            {"tool": "fails", "args": {}},
            {"tool": "ok", "args": {}},
        ])
        self.assertFalse(out["stopped_early"])
        self.assertEqual(out["ran"], 2)

    def test_raised_exception_becomes_a_step_error(self):
        out = self.run_batch(steps=[{"tool": "raises", "args": {}}])
        self.assertIn("boom", out["results"][0]["error"])

    def test_unknown_tool_is_reported_not_called(self):
        out = self.run_batch(steps=[{"tool": "nosuch", "args": {}}])
        self.assertEqual(out["results"][0]["error"], "unknown tool")
        self.assertEqual(self.mcp._tool_manager.calls, [])

    def test_nesting_and_pre_turn_check_are_denied(self):
        for name in ("batch", "pre_turn_check"):
            out = self.run_batch(steps=[{"tool": name, "args": {}}])
            self.assertIn("cannot run inside batch", out["results"][0]["error"])
            self.assertEqual(self.mcp._tool_manager.calls, [])

    def test_bad_args_shape_is_reported(self):
        out = self.run_batch(steps=[{"tool": "ok", "args": "not-a-dict"}])
        self.assertEqual(out["results"][0]["error"], "args must be an object")


if __name__ == "__main__":
    unittest.main()
