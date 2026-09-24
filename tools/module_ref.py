"""module_lookup — search the published module the campaign is running.

Campaigns that run a module keep its text under campaigns/<slug>/reference/
as markdown. The DM used to read those files with Bash (or not at all, and
improvise); this indexes them into an in-memory SQLite FTS5 table, one row
per paragraph or numbered key entry, tagged with its heading path. The index
is rebuilt when any reference file's mtime changes.

DM-only material: module text is never narrated verbatim and never leaks
into player-facing choices (CLAUDE.md, Hard Constraint #5).
"""
from __future__ import annotations

import re
import sqlite3

import _campaign as _c

_index: dict[str, tuple[tuple, sqlite3.Connection]] = {}
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_BOLD_HEADING = re.compile(r"^\*\*(.+?)\*\*\s*$")
_KEY_ENTRY = re.compile(r"^\d+[a-z]?\.\s")


def _chunks(text: str) -> list[tuple[str, str]]:
    """(heading path, chunk) pairs. A chunk is a paragraph, or one numbered
    key entry with its wrapped continuation lines."""
    path: list[tuple[int, str]] = []
    out: list[tuple[str, str]] = []
    buf: list[str] = []

    def flush():
        if buf:
            out.append((" › ".join(t for _, t in path), " ".join(buf).strip()))
            buf.clear()

    for raw in text.splitlines():
        line = raw.strip()
        m = _HEADING.match(line)
        b = _BOLD_HEADING.match(line)
        if m or b:
            flush()
            level = len(m.group(1)) if m else 7
            title = (m.group(2) if m else b.group(1)).strip()
            while path and path[-1][0] >= level:
                path.pop()
            path.append((level, title))
            continue
        if not line:
            flush()
            continue
        if _KEY_ENTRY.match(line):
            flush()
        buf.append(line)
    flush()
    return out


def _db(cfg: dict) -> sqlite3.Connection | None:
    ref = cfg["_dir"] / "reference"
    files = sorted(ref.glob("*.md")) if ref.is_dir() else []
    if not files:
        return None
    sig = (str(cfg["_dir"]),) + tuple((f.name, f.stat().st_mtime) for f in files)
    key = str(cfg["_dir"])
    cached = _index.get(key)
    if cached and cached[0] == sig:
        return cached[1]
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.execute("CREATE VIRTUAL TABLE chunks USING fts5(file, path, text)")
    for f in files:
        rows = [(f.name, p, t) for p, t in _chunks(f.read_text(encoding="utf-8"))]
        con.executemany("INSERT INTO chunks VALUES (?, ?, ?)", rows)
    _index[key] = (sig, con)
    return con


def _fts_query(q: str) -> str:
    """Each word as a quoted term, ANDed — user text is never FTS syntax."""
    words = re.findall(r"[\w'-]+", q)
    return " ".join('"' + w.replace('"', "") + '"' for w in words)


def _like_pattern(section: str) -> str:
    """A `%section%` LIKE pattern with %, _ and \\ escaped so section text
    is never LIKE syntax."""
    escaped = section.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return "%" + escaped + "%"


def register(mcp):

    @mcp.tool()
    def module_lookup(query: str, section: str = "", limit: int = 5) -> dict:
        """Search the published module this campaign runs (its reference/
        text): room keys, level overviews, NPCs, wandering-monster tables.

        query:   words to find, e.g. "fire beetles", "gentlemen ghouls".
        section: only results whose heading path contains this, e.g.
                 "1B", "Level Two", "Room Key".
        DM-only. The dungml map already carries most room keys as each
        room's boxed text and DM notes (move_party / room return them);
        reach for this for what the map doesn't hold."""
        cfg = _c.load_campaign()
        con = _db(cfg)
        if con is None:
            return {"results": [], "note": "this campaign has no reference/ module text"}
        fts = _fts_query(query)
        if not fts:
            return {"results": []}
        n = max(1, int(limit))
        try:
            if section:
                rows = con.execute(
                    "SELECT file, path, text FROM chunks "
                    "WHERE chunks MATCH ? AND path LIKE ? ESCAPE '\\' "
                    "ORDER BY rank LIMIT ?",
                    (fts, _like_pattern(section), n)).fetchall()
            else:
                rows = con.execute(
                    "SELECT file, path, text FROM chunks WHERE chunks MATCH ? "
                    "ORDER BY rank LIMIT ?",
                    (fts, n)).fetchall()
        except sqlite3.OperationalError:
            return {"results": []}
        return {"results": [{"file": f, "path": p, "text": t}
                            for f, p, t in rows]}
