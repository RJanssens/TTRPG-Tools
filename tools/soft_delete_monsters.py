#!/usr/bin/env python3
"""Soft-delete (or restore) entries in global/monsters.db.

A soft-deleted monster keeps its row — so a wiki re-import sees the name as
already recorded and never brings it back — but carries a `deleted_at` stamp
and a `deleted_reason`, and every lookup (monster_lookup, setup_combat, the
encounter-table builder, the /monsters browser) skips it.

Usage:
    python3 tools/soft_delete_monsters.py --reason "April-fool joke" NAME [NAME...]
    python3 tools/soft_delete_monsters.py --restore NAME [NAME...]
    python3 tools/soft_delete_monsters.py --list
Names match exactly, case-insensitively.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = Path(__file__).resolve().parent.parent / "global" / "monsters.db"


def ensure_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(monsters)")}
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN deleted_at TEXT")
    if "deleted_reason" not in cols:
        conn.execute("ALTER TABLE monsters ADD COLUMN deleted_reason TEXT")


def soft_delete(conn, names: list[str], reason: str) -> tuple[list[str], list[str]]:
    ensure_columns(conn)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    done, missing = [], []
    for n in names:
        cur = conn.execute(
            "UPDATE monsters SET deleted_at = ?, deleted_reason = ? "
            "WHERE lower(trim(name)) = lower(trim(?)) AND deleted_at IS NULL",
            (stamp, reason, n))
        (done if cur.rowcount else missing).append(n)
    return done, missing


def restore(conn, names: list[str]) -> tuple[list[str], list[str]]:
    ensure_columns(conn)
    done, missing = [], []
    for n in names:
        cur = conn.execute(
            "UPDATE monsters SET deleted_at = NULL, deleted_reason = NULL "
            "WHERE lower(trim(name)) = lower(trim(?)) AND deleted_at IS NOT NULL",
            (n,))
        (done if cur.rowcount else missing).append(n)
    return done, missing


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*")
    ap.add_argument("--reason", default="")
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db)
    try:
        ensure_columns(conn)
        if args.list:
            for name, when, why in conn.execute(
                    "SELECT name, deleted_at, deleted_reason FROM monsters "
                    "WHERE deleted_at IS NOT NULL ORDER BY name"):
                print(f"{name}  [{when}]  {why or ''}")
            return 0
        if not args.names:
            ap.error("give one or more monster names (or --list)")
        if args.restore:
            done, missing = restore(conn, args.names)
        else:
            if not args.reason:
                ap.error("--reason is required when deleting")
            done, missing = soft_delete(conn, args.names, args.reason)
        conn.commit()
        verb = "restored" if args.restore else "soft-deleted"
        print(f"{verb} {len(done)}: {', '.join(done)}")
        if missing:
            print(f"not found (or already {verb}): {', '.join(missing)}", file=sys.stderr)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
