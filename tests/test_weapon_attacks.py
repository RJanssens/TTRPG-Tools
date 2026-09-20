"""Unit tests for weapon attack blocks, damage dice and healing rates.

Covers the two bugs these fixes closed:

  * `char["weapon"]` holds prose ("Battle axe"), so the old damage parser fell
    through to `return 1` and every PC in five of six campaigns hit for 1 plus
    their bonus, while the sheets rendered the correct numbers from a field the
    tracker never read.
  * memorised spells were stored at level 0, so cast_spell's slot decrement was
    skipped and the spell economy was free.

Run: python3 -m unittest tests.test_weapon_attacks
"""

from __future__ import annotations

import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import combat
from tools import party


class DiceParsingTests(unittest.TestCase):
    def test_valid_notations(self):
        self.assertEqual(combat._parse_dice("1d8"), (1, 8, 0))
        self.assertEqual(combat._parse_dice("d6"), (1, 6, 0))
        self.assertEqual(combat._parse_dice("2d4"), (2, 4, 0))
        self.assertEqual(combat._parse_dice("1d8+6"), (1, 8, 6))
        self.assertEqual(combat._parse_dice("1d3-1"), (1, 3, -1))
        self.assertEqual(combat._parse_dice("1d6 + 1"), (1, 6, 1))

    def test_flat_damage(self):
        self.assertEqual(combat._parse_dice("1"), (0, 0, 1))
        self.assertEqual(combat._roll_dice("3"), 3)

    def test_prose_is_rejected_not_silently_rolled(self):
        for bad in ("Battle axe", "Maragbur — bastard sword +1", "Footman's mace",
                    "1d3+caster_lvl", "", None, "Nil"):
            self.assertIsNone(combat._parse_dice(bad), bad)
            with self.assertRaises(ValueError):
                combat._roll_dice(bad)

    def test_roll_respects_bounds(self):
        random.seed(7)
        for _ in range(200):
            self.assertIn(combat._roll_dice("1d8+6"), range(7, 15))


class SizeTests(unittest.TestCase):
    def test_size_codes(self):
        self.assertEqual(combat._size_code("Small (4' tall)"), "S")
        self.assertEqual(combat._size_code("Large (9' tall)"), "L")
        self.assertEqual(combat._size_code("Gargantuan"), "G")
        self.assertEqual(combat._size_code("M"), "M")

    def test_unknown_size_defaults_to_man_sized(self):
        # Defaulting to M selects the S/M die — the conservative choice.
        for raw in ("", None, "Varies", "?"):
            self.assertEqual(combat._size_code(raw), "M")
            self.assertFalse(combat._takes_large_damage(combat._size_code(raw)))

    def test_large_and_bigger_take_the_large_die(self):
        self.assertTrue(combat._takes_large_damage("L"))
        self.assertTrue(combat._takes_large_damage("H"))
        self.assertTrue(combat._takes_large_damage("G"))
        for small in ("T", "S", "M"):
            self.assertFalse(combat._takes_large_damage(small))


class AttackBlockTests(unittest.TestCase):
    def test_normalizes_a_sheet_block(self):
        blocks, warnings = combat._normalize_attacks({
            "label": "Garrek",
            "attacks": [{"name": "Sunsweep long sword +1", "speed": 5,
                         "attacks": 1.5, "thac0": 13,
                         "damage_sm": "1d8+6", "damage_l": "1d12+6"}],
        })
        self.assertEqual(warnings, [])
        b = blocks[0]
        self.assertEqual((b["thac0"], b["damage_sm"], b["damage_l"], b["speed"], b["rate"]),
                         (13, "1d8+6", "1d12+6", 5, 1.5))

    def test_missing_large_die_falls_back_to_sm(self):
        blocks, _ = combat._normalize_attacks({
            "label": "X", "attacks": [{"name": "club", "damage_sm": "1d6"}]})
        self.assertEqual(blocks[0]["damage_l"], "1d6")

    def test_unusable_damage_is_dropped_and_reported(self):
        blocks, warnings = combat._normalize_attacks({
            "label": "Aelarion",
            "attacks": [{"name": "Burning Hands", "damage_sm": "1d3+caster_lvl"},
                        {"name": "Quarterstaff", "damage_sm": "1d6"}],
        })
        self.assertEqual([b["name"] for b in blocks], ["Quarterstaff"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("Burning Hands", warnings[0])

    def test_applying_a_block_zeroes_the_generic_bonuses(self):
        # A block's THAC0 and damage already include STR, magic and
        # specialisation; adding bonus_hit/bonus_dmg again would double-count.
        c = {"thac0": 16, "bonus_hit": 2, "bonus_dmg": 3}
        combat._apply_attack_block(c, {"name": "sword", "thac0": 13,
                                       "damage_sm": "1d8+6", "damage_l": "1d12+6",
                                       "speed": 5, "rate": 1.5})
        self.assertEqual((c["thac0"], c["bonus_hit"], c["bonus_dmg"]), (13, 0, 0))
        self.assertEqual((c["dmg"], c["dmg_large"]), ("1d8+6", "1d12+6"))

    def test_select_attack_by_substring_prefers_shortest_match(self):
        c = {"name": "Garrek", "attack_blocks": [
            {"name": "Sunsweep long sword +1 (vs normal)"},
            {"name": "Sunsweep long sword +3 (vs undead)"},
            {"name": "Dagger (melee)"},
        ]}
        block, note = combat._select_attack(c, "undead")
        self.assertIn("undead", block["name"])
        self.assertEqual(note, "")
        block, note = combat._select_attack(c, "Dagger (melee)")
        self.assertEqual(block["name"], "Dagger (melee)")

    def test_select_attack_reports_a_miss_without_changing_weapon(self):
        c = {"name": "Garrek", "weapon": "Long sword",
             "attack_blocks": [{"name": "Long sword"}]}
        block, note = combat._select_attack(c, "halberd")
        self.assertIsNone(block)
        self.assertIn("No attack matching", note)
        self.assertIn("Long sword", note)


class AttackRateTests(unittest.TestCase):
    def test_rate_parsing(self):
        self.assertEqual(combat._attack_rate({"attacks": 1.5}), 1.5)
        self.assertEqual(combat._attack_rate({"attacks": "3/2"}), 1.5)
        self.assertEqual(combat._attack_rate({"attacks": "2"}), 2.0)
        self.assertEqual(combat._attack_rate({"attacks": None}), 1.0)
        self.assertEqual(combat._attack_rate({"attacks": "nonsense"}), 1.0)

    def test_three_halves_alternates_one_and_two(self):
        c = {"attacks_per_round": 1.5}
        got = [combat._attacks_allowed_in_round(c, r) for r in range(1, 7)]
        self.assertEqual(got, [1, 2, 1, 2, 1, 2])

    def test_whole_rates_are_constant(self):
        for rate, expect in ((1, 1), (2, 2), (3, 3)):
            c = {"attacks_per_round": rate}
            self.assertEqual(
                [combat._attacks_allowed_in_round(c, r) for r in range(1, 5)],
                [expect] * 4,
            )

    def test_entitlement_is_never_banked_across_rounds(self):
        # A fighter who spends a round unconscious must not come back with
        # three swings, which is why the allowance is per-round arithmetic
        # rather than a lifetime counter.
        c = {"attacks_per_round": 1.5, "attacks_this_round": 0}
        self.assertEqual(combat._attacks_remaining_this_round(c, 5), 1)
        c["attacks_this_round"] = 1
        self.assertEqual(combat._attacks_remaining_this_round(c, 5), 0)
        self.assertEqual(combat._attacks_remaining_this_round(c, 6), 1)


class SpellLevelTests(unittest.TestCase):
    def test_caster_kind(self):
        self.assertEqual(party._caster_kind("Cleric"), "priest")
        self.assertEqual(party._caster_kind("Mage"), "wizard")
        self.assertEqual(party._caster_kind("Fighter"), "")

    def test_bare_names_get_levels_resolved(self):
        out = party._normalize_memorized(
            ["Cure Light Wounds", "Hold Person"], "priest")
        self.assertEqual([s["level"] for s in out], [1, 2])
        self.assertTrue(all(s["cast"] is False for s in out))

    def test_explicit_level_is_respected(self):
        out = party._normalize_memorized([{"name": "Homebrew Ward", "level": 3}], "priest")
        self.assertEqual(out[0]["level"], 3)

    def test_unknown_spell_stays_level_zero(self):
        out = party._normalize_memorized(["Blorp of Doom"], "wizard")
        self.assertEqual(out[0]["level"], 0)

    def test_cast_flag_survives_normalisation(self):
        out = party._normalize_memorized(
            [{"name": "Bless", "level": 1, "cast": True}], "priest")
        self.assertTrue(out[0]["cast"])


class ConHpBonusTests(unittest.TestCase):
    def test_warrior_gets_the_parenthetical_value(self):
        # CON 17 reads "+2(+3)": +2 for anyone, +3 for warriors.
        self.assertEqual(party._con_hp_bonus(
            {"cls": "Fighter", "ability_scores": {"con": 17}}), 3)
        self.assertEqual(party._con_hp_bonus(
            {"cls": "Thief", "ability_scores": {"con": 17}}), 2)

    def test_average_constitution_gives_nothing(self):
        self.assertEqual(party._con_hp_bonus(
            {"cls": "Fighter", "ability_scores": {"con": 9}}), 0)

    def test_missing_score_is_zero_not_an_error(self):
        self.assertEqual(party._con_hp_bonus({"cls": "Fighter"}), 0)


if __name__ == "__main__":
    unittest.main()
