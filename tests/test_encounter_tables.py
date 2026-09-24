"""Unit tests for the tiered encounter-table builder.

Run: python3 -m unittest tests.test_encounter_tables

Regression origin: a 1st-level party in night-haunted-halls rolled
"2-6 Hound Of Kyuss" (HD 2+2, XP 650, climate "Empire of Iuz, Rift Canyon")
off the *mountain low* tier. Four separate defects put it there, and each has
a test below.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import build_encounter_tables as bet


class TerrainMappingTests(unittest.TestCase):
    """climate_terrain -> {terrain: specificity}."""

    def test_canyon_does_not_count_as_any_terrain(self):
        """'Rift Canyon' contains the substring 'any' — it is not "any land".

        The Hound of Kyuss is native to the Empire of Iuz; it belongs in no
        generic terrain table at all.
        """
        self.assertEqual(bet.terrains_for("Empire of Iuz, Rift Canyon"), {})

    def test_other_any_substring_words_do_not_spread(self):
        for climate in ("Botany gardens of Zul", "The Company lands"):
            with self.subTest(climate=climate):
                self.assertEqual(bet.terrains_for(climate), {})

    def test_any_land_still_spreads_across_land_terrains(self):
        got = bet.terrains_for("Any land")
        for t in bet._LAND_TERRAINS:
            self.assertEqual(got.get(t), 1, t)
        self.assertEqual(got.get("wilderness"), 1)
        self.assertEqual(got.get("road"), 1)

    def test_anywhere_is_an_any_token(self):
        """'Anywhere' means any land — the word-boundary rule must not eat it."""
        self.assertIn("forest", bet.terrains_for("Anywhere"))
        self.assertIn("plains", bet.terrains_for("Anywhere on land"))

    def test_parenthesised_any_still_spreads(self):
        self.assertIn("plains", bet.terrains_for("Outlands (any plains)"))

    def test_specific_terrain_still_wins(self):
        got = bet.terrains_for("Temperate forest")
        self.assertEqual(got.get("forest"), 3)
        self.assertNotIn("arctic", got)


class HitDiceParsingTests(unittest.TestCase):
    """parse_hd must not read a hit-*point* figure as hit *dice*."""

    def test_hp_style_fields_are_not_hit_dice(self):
        for field in ("1d8+76 hp", "1 hp", "1-2 hp", "1d4 hp", "(1d4 hp)",
                      "2 hp", "1 hp per bloom", "1-6 hp or by class and level"):
            with self.subTest(field=field):
                self.assertIsNone(bet.parse_hd(field))

    def test_real_hit_dice_still_parse(self):
        self.assertEqual(bet.parse_hd("2+2"), 2)
        self.assertEqual(bet.parse_hd("3+1 (hp 27)"), 3)
        self.assertEqual(bet.parse_hd("1 (4hp)"), 1)
        self.assertEqual(bet.parse_hd("8+2"), 8)

    def test_fractional_hit_dice_band_low(self):
        self.assertEqual(bet.parse_hd("1/2 (1hp)"), 0)
        self.assertEqual(bet.parse_hd("½"), 0)

    def test_prose_hit_dice_unparseable(self):
        self.assertIsNone(bet.parse_hd("See below"))
        self.assertIsNone(bet.parse_hd(None))


class NumberAppearingTests(unittest.TestCase):
    """'2d6' means 2-12, not 2-6."""

    def test_dice_notation_expands_to_true_range(self):
        self.assertEqual(bet.number_appearing("2d6", 0), "2-12")
        self.assertEqual(bet.number_appearing("3d4", 0), "3-12")
        self.assertEqual(bet.number_appearing("1d10", 0), "1-10")
        self.assertEqual(bet.number_appearing("1d4", 0), "1-4")

    def test_dice_notation_respects_band_cap(self):
        self.assertEqual(bet.number_appearing("5d10", 0), "5-20")   # cap 20
        self.assertEqual(bet.number_appearing("2d6", 3), "2-3")     # cap 3

    def test_plain_ranges_unchanged(self):
        self.assertEqual(bet.number_appearing("2-12", 0), "2-12")
        self.assertEqual(bet.number_appearing("1", 0), "1")
        self.assertEqual(bet.number_appearing(None, 0), "1")

    def test_mixed_notation_takes_widest_span(self):
        self.assertEqual(bet.number_appearing("1d6 (2d10)", 0), "1-20")


class BandingTests(unittest.TestCase):
    """Hit dice alone understate a monster with nasty specials; XP does not."""

    def test_xp_promotes_a_low_hd_monster_out_of_the_low_tier(self):
        # Hound of Kyuss: HD 2+2 (band low) but 650 XP (band mid).
        band, hd = bet.band_for("2+2", "650")
        self.assertEqual(band, 1)
        self.assertEqual(hd, 2)

    def test_hp_style_hit_dice_fall_back_to_xp(self):
        # Death kiss: HD "1d8+76 hp", XP 8,000 -> epic, not low.
        band, hd = bet.band_for("1d8+76 hp", "8,000")
        self.assertEqual(band, 3)

    def test_ordinary_low_level_monster_stays_low(self):
        band, hd = bet.band_for("1", "15")      # orc
        self.assertEqual(band, 0)
        self.assertEqual(hd, 1)

    def test_hit_dice_win_when_they_are_the_harsher_read(self):
        # A high-HD, low-XP creature must not be demoted by its XP.
        band, hd = bet.band_for("10", "15")
        self.assertEqual(band, 2)

    def test_unbandable_row_returns_none(self):
        self.assertIsNone(bet.band_for("See below", "Varies"))


class SourceWeightTests(unittest.TestCase):
    """Traditional D&D monsters should come up more often than obscurities.

    monsters.db holds two disjoint imports: a curated core bestiary (ids 1-560,
    no `categories` at all — orc, goblin, kobold, wolf, skeleton, ogre) and a
    bulk wiki import (ids 900000+, always categorised). So an EMPTY categories
    field is the core-import signal, and the wiki rows are graded by the source
    appendix they name.
    """

    def test_curated_core_import_is_traditional(self):
        """orc/goblin/skeleton carry no categories — they must not score 1."""
        self.assertEqual(bet.source_weight(None), bet._CORE_SOURCE_MULT)
        self.assertEqual(bet.source_weight("[]"), bet._CORE_SOURCE_MULT)

    def test_monstrous_manual_tag_is_also_traditional(self):
        self.assertEqual(
            bet.source_weight(json.dumps(["Creatures", "Monstrous Manual Creatures"])),
            bet._CORE_SOURCE_MULT)

    def test_core_bestiary_outweighs_official_setting_appendix(self):
        core = bet.source_weight(json.dumps(["Creatures", "Monstrous Manual Creatures"]))
        official = bet.source_weight(json.dumps(["Creatures", "Greyhawk Appendix Creatures"]))
        self.assertGreater(core, official)

    def test_official_setting_appendix_outweighs_magazine_oddity(self):
        official = bet.source_weight(json.dumps(["Creatures", "Annual Volume One Creatures"]))
        obscure = bet.source_weight(json.dumps(["Creatures", "Dragon Magazine Creatures"]))
        self.assertGreater(official, obscure)

    def test_wiki_catch_all_category_is_not_a_core_signal(self):
        """'Creatures' sits on 2,252 of 2,724 rows and carries no signal."""
        self.assertEqual(bet.source_weight(json.dumps(["Creatures"])), 1)
        self.assertEqual(
            bet.source_weight(json.dumps(["Creatures", "Polyhedron Magazine Creatures"])), 1)

    def test_fiend_folio_and_compendium_volumes_are_core(self):
        for cat in ("Fiend Folio Appendix Creatures",
                    "Compendium Volume One Creatures",
                    "Compendium Volume Two Creatures"):
            with self.subTest(cat=cat):
                self.assertEqual(bet.source_weight(json.dumps([cat])),
                                 bet._CORE_SOURCE_MULT)


class RarityWeightTests(unittest.TestCase):
    """Rare monsters should be rare — rarity is the dominant lever."""

    def test_rarity_is_strictly_ordered(self):
        w = [bet.freq_weight(f) for f in
             ("Very rare", "Rare", "Uncommon", "Common")]
        self.assertEqual(w, sorted(w))
        self.assertEqual(len(set(w)), 4)

    def test_a_common_far_outweighs_a_very_rare(self):
        self.assertGreaterEqual(bet.freq_weight("Common"),
                                bet.freq_weight("Very rare") * 32)

    def test_rarity_outranks_terrain_fit(self):
        """Terrain fit orders peers; it can never promote a rarity tier."""
        best_boost = max(bet._SPECIFICITY_MULT.values())
        self.assertGreater(bet.freq_weight("Rare"),
                           bet.freq_weight("Very rare") * best_boost)
        self.assertGreater(bet.freq_weight("Uncommon"),
                           bet.freq_weight("Rare") * best_boost)
        self.assertGreater(bet.freq_weight("Common"),
                           bet.freq_weight("Uncommon") * best_boost)

    def test_compound_frequency_strings_read_as_the_rarer_half(self):
        self.assertEqual(bet.freq_weight("Very rare (uncommon)"),
                         bet.freq_weight("Very rare"))
        self.assertEqual(bet.freq_weight("Uncommon"), bet.freq_weight("uncommon"))


def _fixture_db(path: Path) -> None:
    """A three-monster bestiary covering the cases above."""
    cols = ("name", "frequency", "no_appearing", "hit_dice", "climate_terrain",
            "intelligence", "description", "xp_value", "categories")
    rows = [
        # region-locked, low HD, mid XP -> must not appear anywhere
        ("hound of kyuss", "Rare", "2d6", "2+2", "Empire of Iuz, Rift Canyon",
         "Non- (0)", "undead scarab", "650", json.dumps(["Creatures", "Dragon Magazine Creatures"])),
        # traditional staple
        ("orc", "Common", "2d4", "1", "Any land", "Average (8)", "pig-faced",
         "15", json.dumps(["Creatures", "Monstrous Manual Creatures"])),
        # obscure rarity, same terrain and band
        ("firestar", "Very rare", "1d3", "2+2", "Temperate forest",
         "Animal (1)", "burning mote", "2000", json.dumps(["Creatures", "Dragon Magazine Creatures"])),
    ]
    con = sqlite3.connect(str(path))
    with con:
        con.execute(f"CREATE TABLE monsters ({', '.join(c + ' TEXT' for c in cols)})")
        con.executemany(
            f"INSERT INTO monsters ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
            rows)
    con.close()


class BuildIntegrationTests(unittest.TestCase):
    """End-to-end over a fixture bestiary."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        db = Path(cls._tmp.name) / "monsters.db"
        _fixture_db(db)
        cls.data = bet.build(db, max_per_table=120)

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def _entries(self, terrain: str, band: int) -> list[dict]:
        return self.data["terrains"][terrain]["tiers"][band]

    def _width(self, terrain: str, band: int, monster: str) -> int:
        for e in self._entries(terrain, band):
            if e["monster"] == monster:
                lo, hi = e["range"]
                return hi - lo + 1
        return 0

    def test_region_locked_monster_is_in_no_table(self):
        blob = json.dumps(self.data["terrains"])
        self.assertNotIn("kyuss", blob.lower())

    def test_traditional_staple_dominates_its_tier(self):
        width = self._width("forest", 0, "orc")
        total = max(e["range"][1] for e in self._entries("forest", 0))
        self.assertGreater(width / total, 0.9)

    def test_obscure_rarity_is_promoted_out_of_the_low_tier_by_xp(self):
        # firestar: HD 2+2 but 2000 XP -> high tier, not low.
        self.assertEqual(self._width("forest", 0, "firestar"), 0)
        self.assertGreater(self._width("forest", 2, "firestar"), 0)

    def test_number_appearing_uses_true_dice_range(self):
        results = [e["result"] for e in self._entries("forest", 0)]
        self.assertIn("2-8 Orc", results)


if __name__ == "__main__":
    unittest.main()
