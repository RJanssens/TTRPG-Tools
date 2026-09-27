"""XP tables and level progress, single- and multi-class.

A multi-class character ("Fighter/Thief") keeps one XP total in state.json.
Under the PHB rule every award is split evenly between the classes, so each
class's share is the total divided by the number of classes. Each class then
levels on its own table: a Fighter/Thief with 2,600 XP has 1,300 in each —
Thief 2, Fighter 1.

The sheet records the per-class levels in `levels` ({"fighter": 1, "thief": 2});
a sheet without it has every class at its single `level`.

No Flask here — the MCP tools and the dashboard both import it.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from tools import homebrew_classes as _hb

_2E_DB = Path(__file__).resolve().parent.parent / "global" / "2e.db"

# 2e kits and common synonyms map onto their PHB base class for XP-table
# lookup. The kits use the base class's progression in the published rules.
CLASS_ALIASES: dict[str, str] = {
    "barbarian":   "Fighter",   # Complete Barbarian's Handbook (Fighter kit)
    "berserker":   "Fighter",
    "cavalier":    "Fighter",
    "gladiator":   "Fighter",
    "samurai":     "Fighter",
    "wizard":      "Mage",
    "necromancer": "Specialist Mage",
    "abjurer":     "Specialist Mage",
    "conjurer":    "Specialist Mage",
    "diviner":     "Specialist Mage",
    "enchanter":   "Specialist Mage",
    "illusionist": "Specialist Mage",
    "invoker":     "Specialist Mage",
    "transmuter":  "Specialist Mage",
    "monk":        "Cleric",    # Scarlet Brotherhood / OA-style monk uses Priest table
    "assassin":    "Thief",     # 2e: Assassin kit of Thief
    "swashbuckler":"Thief",
}

_cache: dict[str, list[int]] = {}


def xp_table(cls: str) -> list[int]:
    """[xp_required at L1, L2, …] for ONE class, or [] if unknown.

    Homebrew classes win, then PHB classes in global/2e.db, then the kit alias.
    A multi-class string has no table of its own — use `progress`."""
    key = (cls or "").strip().lower()
    if not key:
        return []
    if key in _cache:
        return _cache[key]

    table: list[int] = _hb.homebrew_xp_table(key)
    if not table and _2E_DB.exists():
        candidates = [key]
        if key in CLASS_ALIASES:
            candidates.append(CLASS_ALIASES[key].lower())
        try:
            conn = sqlite3.connect(f"file:{_2E_DB}?mode=ro", uri=True)
            try:
                for candidate in candidates:
                    row = conn.execute(
                        "SELECT id FROM classes WHERE lower(name) = ? LIMIT 1", (candidate,),
                    ).fetchone()
                    if row is None:
                        row = conn.execute(
                            "SELECT id FROM classes WHERE lower(name) LIKE ? LIMIT 1",
                            (candidate + "%",),
                        ).fetchone()
                    if row is not None:
                        table = [int(r[0]) for r in conn.execute(
                            "SELECT xp_required FROM class_levels WHERE class_id=? "
                            "ORDER BY level", (row[0],),
                        ).fetchall()]
                        break
            finally:
                conn.close()
        except sqlite3.Error:
            table = []

    _cache[key] = table
    return table


def component_classes(cls: str) -> list[str]:
    """'Fighter/Thief' → ['fighter', 'thief']; 'Mage' → ['mage']."""
    return [p.strip().lower() for p in re.split(r"/", cls or "") if p.strip()]


def is_multiclass(char: dict) -> bool:
    return len(component_classes(char.get("cls", ""))) > 1


def class_levels(char: dict) -> dict[str, int]:
    """Per-class levels, in the order the class string names them."""
    classes = component_classes(char.get("cls", ""))
    stored = {str(k).strip().lower(): int(v)
              for k, v in (char.get("levels") or {}).items()}
    base = int(char.get("level") or 1)
    return {c: stored.get(c, base) for c in classes}


def level_label(char: dict) -> str:
    """'1/2' for a Fighter 1 / Thief 2, '5' for a single class, '' if none."""
    if is_multiclass(char):
        return "/".join(str(v) for v in class_levels(char).values())
    level = char.get("level")
    return str(level) if level not in (None, "") else ""


def progress(char: dict, xp_total: int) -> list[dict]:
    """One row per class: its XP share, current level, the share needed for
    its next level, and — so a caller can compare against the single stored
    total — `total_next`, the XP total at which that class levels.

    `earned_level` is the highest level the share already reaches on that
    class's table; `ready` is true when it is above the recorded level."""
    levels = class_levels(char)
    n = max(len(levels), 1)
    share = int(xp_total) // n
    rows = []
    for cls, level in levels.items():
        table = xp_table(cls)
        row: dict = {"class": cls.title(), "level": level, "xp": share,
                     "xp_floor": None, "xp_next": None, "total_next": None,
                     "earned_level": None, "ready": False}
        if table:
            row["xp_floor"] = table[level - 1] if 1 <= level <= len(table) else None
            if level < len(table):
                row["xp_next"] = table[level]
                row["total_next"] = table[level] * n
            earned = sum(1 for t in table if t <= share)
            row["earned_level"] = earned
            row["ready"] = earned > level
        rows.append(row)
    return rows


def level_up_hints(char: dict, xp_before: int, xp_after: int) -> list[str]:
    """One line per class whose share crossed a threshold with this award."""
    label = char.get("label", "?")
    multi = is_multiclass(char)
    before = {r["class"]: r["earned_level"] for r in progress(char, xp_before)}
    hints = []
    for r in progress(char, xp_after):
        was, now = before.get(r["class"]), r["earned_level"]
        if was is None or now is None or now <= was or now <= r["level"]:
            continue
        share = f" ({r['xp']:,} XP share)" if multi else ""
        hints.append(f"Level up! {label} is eligible for {r['class']} level {now}{share}.")
    return hints


def bar(char: dict, xp_total: int) -> tuple[int, int | None]:
    """(floor, next) on the XP-total scale, for a single progress bar.

    Single class: the table values. Multi-class: the class that levels
    soonest, scaled back up to the total (share × number of classes)."""
    rows = progress(char, xp_total)
    n = max(len(rows), 1)
    live = [r for r in rows if r["total_next"] is not None]
    if not live:
        floor = max(((r["xp_floor"] or 0) * n for r in rows), default=0)
        return floor, None
    soonest = min(live, key=lambda r: r["total_next"])
    return (soonest["xp_floor"] or 0) * n, soonest["total_next"]
