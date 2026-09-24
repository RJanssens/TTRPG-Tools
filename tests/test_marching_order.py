"""Marching order is the roster order: campaign.json's `characters` key order,
the same sequence the /play sidebar's ▴/▾ handles rearrange.

Run: python3 -m unittest tests.test_marching_order
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _campaign as _c
from tests._campaign_fixture import TempCampaign
from tools import lore, party

PARTY = {"characters": {
    "durgan": {"label": "Durgan", "cls": "Fighter", "hp_max": 10},
    "oswin":  {"label": "Oswin", "cls": "Cleric", "hp_max": 8},
    "aldous": {"label": "Aldous", "cls": "Mage", "hp_max": 4},
}}


class MarchingOrderHelperTests(unittest.TestCase):
    def test_is_the_roster_order(self):
        TempCampaign(self, campaign=PARTY)
        self.assertEqual(_c.marching_order(_c.load_campaign()),
                         ["durgan", "oswin", "aldous"])


class SetMarchingOrderToolTests(unittest.TestCase):
    def setUp(self):
        self.camp = TempCampaign(self, campaign=PARTY)
        self.tools = TempCampaign.register(party, lore)

    def test_accepts_labels_and_persists(self):
        out = self.tools["set_marching_order"](["Oswin", "durgan", "Aldous"])
        self.assertEqual(out["marching_order"], ["Oswin", "Durgan", "Aldous"])
        self.assertEqual(list(self.camp.read_json("campaign.json")["characters"]),
                         ["oswin", "durgan", "aldous"])
        self.assertEqual(self.camp.events()[-1]["type"], "marching_order")

    def test_unknown_name_is_an_error_and_changes_nothing(self):
        out = self.tools["set_marching_order"](["Oswin", "Bob"])
        self.assertIn("error", out)
        self.assertEqual(list(self.camp.read_json("campaign.json")["characters"]),
                         ["durgan", "oswin", "aldous"])

    def test_partial_list_keeps_the_rest_behind(self):
        out = self.tools["set_marching_order"](["Aldous"])
        self.assertEqual(out["marching_order"], ["Aldous", "Durgan", "Oswin"])

    def test_character_records_survive_the_reorder(self):
        self.tools["set_marching_order"](["Aldous"])
        self.assertEqual(self.camp.read_json("campaign.json")["characters"]["aldous"],
                         PARTY["characters"]["aldous"])

    def test_primer_reports_marching_order(self):
        self.tools["set_marching_order"](["Oswin", "Durgan", "Aldous"])
        out = self.tools["session_primer"](scene=False)
        self.assertEqual(out["marching_order"], ["Oswin", "Durgan", "Aldous"])
        self.assertEqual(list(out["party"]), ["oswin", "durgan", "aldous"])


if __name__ == "__main__":
    unittest.main()
