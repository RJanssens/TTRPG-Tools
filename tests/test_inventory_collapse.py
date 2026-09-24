"""collapse_inventory — folding repeated gear lines into 'Name x N'."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.item_weights import collapse_inventory, split_inventory_qty


class TestCollapseInventory(unittest.TestCase):

    def test_repeats_collapse(self):
        self.assertEqual(collapse_inventory(["Arrow"] * 24), ["Arrow x24"])

    def test_singleton_keeps_no_suffix(self):
        self.assertEqual(collapse_inventory(["Long sword"]), ["Long sword"])

    def test_existing_suffix_is_folded_in(self):
        self.assertEqual(collapse_inventory(["Torch x2", "Torch"]), ["Torch x3"])
        self.assertEqual(collapse_inventory(["Torch x2", "Torch x4"]), ["Torch x6"])

    def test_first_appearance_order_and_spelling_preserved(self):
        self.assertEqual(
            collapse_inventory(["Backpack", "Arrow", "Quiver", "arrow", "ARROW"]),
            ["Backpack", "Arrow x3", "Quiver"],
        )

    def test_whitespace_insensitive_match(self):
        # Matching ignores inner whitespace; the first spelling is kept verbatim.
        self.assertEqual(collapse_inventory(["Large  sack", "Large sack"]),
                         ["Large  sack x2"])

    def test_blanks_dropped(self):
        self.assertEqual(collapse_inventory(["", "  ", "Waterskin"]), ["Waterskin"])

    def test_empty_and_none(self):
        self.assertEqual(collapse_inventory([]), [])
        self.assertEqual(collapse_inventory(None), [])

    def test_idempotent(self):
        once = collapse_inventory(["Arrow"] * 12 + ["Bow"])
        self.assertEqual(collapse_inventory(once), once)

    def test_no_item_count_is_lost(self):
        raw = ["Arrow"] * 23 + ["Pole 10 ft"] * 2 + ["Large sack"] * 3 + ["Torch"] * 6
        total = sum(split_inventory_qty(i)[1] for i in collapse_inventory(raw))
        self.assertEqual(total, len(raw))


if __name__ == "__main__":
    unittest.main()
