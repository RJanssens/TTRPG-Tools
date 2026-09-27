#!/usr/bin/env python3
"""Strip leading stat-block bleed from monsters.description.

Many monster rows have the truncated two-column Monstrous Manual *stat block*
prepended to their ``description`` as garbage, e.g. the orc reads::

    Orc Orog
    (8-9)
    High (10-
    Qx10, S)
    ...
    Shaman, 5th 650
    Orcs are a species of aggressive mammalian carnivores that band together...

The real prose ("Orcs are a species of...") is present — it just has the
column-interleaved stat fragments in front of it. The structured stats already
live in their own columns (armor_class, hit_dice, thac0, …), so the leading
block is pure noise.

This migration finds, per row, the first line that reads as real prose and
drops everything before it. Rows whose description already starts with prose are
left untouched, so it is **idempotent** — a second run is a no-op.

A line counts as prose when it is either a long narrative wrap-line (>=55 chars,
>=9 words, few digits, low repetition) or a short complete sentence (>=5 words,
ends in a period, capitalised, almost no digits). Stat-block lines (``Label:``,
number-dense rows, dice ranges, repeated tokens, short fragments) never match,
so the trim boundary sits exactly at the start of the prose.

Idempotent. A timestamped ``.bak`` of the DB is written before any change unless
``--no-backup`` is given.

Usage:
    python3 tools/clean_monster_descriptions.py [--dry-run] [--db PATH] [--no-backup]
"""
from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "global" / "monsters.db"

_STAT_LABEL = re.compile(
    r"(?i)^(climate/terrain|frequency|organization|activity cycle|diet|"
    r"intelligence|treasure|alignment|no\. appearing|armor class|movement|"
    r"hit dice|thac0|no\. of attacks|damage/attack|special attacks|"
    r"special defenses|magic resistance|size|morale|xp value)\s*:"
)


def _digit_ratio(s: str) -> float:
    return sum(ch.isdigit() for ch in s) / max(len(s), 1)


def _distinct_ratio(s: str) -> float:
    words = s.split()
    return len(set(words)) / max(len(words), 1)


def is_prose(line: str) -> bool:
    """True when the line reads as narrative description rather than a stat-block
    fragment. Two accepted shapes: a long wrap-line, or a short full sentence."""
    s = line.strip()
    if not s or _STAT_LABEL.match(s):
        return False
    long_case = (
        len(s) >= 55
        and len(s.split()) >= 9
        and _digit_ratio(s) < 0.08
        and _distinct_ratio(s) > 0.5
    )
    sentence_case = (
        len(s.split()) >= 5
        and s.endswith(".")
        and s[:1].isupper()
        and _digit_ratio(s) < 0.05
        and _distinct_ratio(s) > 0.6
    )
    return long_case or sentence_case


def clean_description(desc: str) -> str | None:
    """Return the cleaned description, or None when nothing should change.

    Trims every line before the first prose line, drops stray ``` fence lines,
    and collapses runs of blank lines. Returns None when the text already starts
    with prose (idempotent) or has no detectable prose at all (leave untouched)."""
    lines = desc.splitlines()
    start = next((i for i, l in enumerate(lines) if is_prose(l)), None)
    if start is None or start == 0:
        # Already clean, or no prose to anchor on — don't touch it.
        if start is None:
            return None
        kept = lines
    else:
        kept = lines[start:]
    kept = [l for l in kept if l.strip() != "```"]
    out = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return out if out != desc.strip() else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change without writing")
    ap.add_argument("--db", type=Path, default=_DEFAULT_DB, help="path to monsters.db")
    ap.add_argument("--no-backup", action="store_true",
                    help="skip the timestamped .bak copy before writing")
    ap.add_argument("--show", type=int, default=8,
                    help="how many before/after samples to print")
    args = ap.parse_args(argv)

    if not args.db.exists():
        print(f"error: {args.db} not found", file=sys.stderr)
        return 2

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, description FROM monsters").fetchall()

    changes: list[tuple[int, str, str, str]] = []  # id, name, old, new
    for r in rows:
        old = r["description"] or ""
        new = clean_description(old)
        if new is not None:
            changes.append((r["id"], r["name"], old, new))

    print(f"scanned {len(rows)} monsters; {len(changes)} descriptions need cleaning")
    for _id, name, old, new in changes[: args.show]:
        trimmed = len(old.splitlines()) - len(new.splitlines())
        print(f"\n--- {name!r} (-{trimmed} lines) ---")
        print(f"  before: {old.splitlines()[0][:70]!r}")
        print(f"  after:  {new.splitlines()[0][:70]!r}")

    if args.dry_run:
        print("\n[dry-run] no changes written")
        conn.close()
        return 0

    if not changes:
        conn.close()
        return 0

    if not args.no_backup:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        bak = args.db.with_name(f"{args.db.name}.bak-descclean-{stamp}")
        shutil.copy2(args.db, bak)
        print(f"\nbackup written: {bak}")

    conn.executemany(
        "UPDATE monsters SET description = ? WHERE id = ?",
        [(new, _id) for _id, _name, _old, new in changes],
    )
    conn.commit()
    conn.close()
    print(f"updated {len(changes)} rows in {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
