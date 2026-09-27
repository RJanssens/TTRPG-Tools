#!/usr/bin/env python3
"""Import creatures from the AD&D 2e Fandom wiki into global/monsters.db.

Scans https://adnd2e.fandom.com/wiki/Category:Creatures, parses each page's
``{{Creature}}`` infobox + description, and inserts any creature NOT already in
the monsters table (dedup by case-insensitive name). For each newly imported
creature it also downloads the wiki infobox image (if present) into
``global/monsters/<slug>.<ext>`` — the filename convention the dashboard's
``/monsters/portraits/<filename>`` route already discovers.

monsters.db has no builder script (it's hand-maintained), so direct inserts are
safe. Scraped rows get ids from a dedicated high range (>= _ID_BASE) and a
source footer in the description, so they're easy to spot or re-import.

Usage:
    python3 tools/import_wiki_monsters.py [--dry-run] [--limit N]
        [--no-images] [--db PATH] [--img-dir PATH] [--verbose]
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://adnd2e.fandom.com/api.php"
_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB = _REPO_ROOT / "global" / "monsters.db"
_DEFAULT_IMG_DIR = _REPO_ROOT / "global" / "monsters"
_USER_AGENT = "ttrpg2-monster-importer/1.0 (campaign toolkit; local use)"
_SOURCE_TAG = "adnd2e.fandom.com"
_ID_BASE = 900_000
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}

# {{Creature}} infobox field -> monsters.db column.
_FIELD_MAP = {
    "frequency": "frequency",
    "numberappearing": "no_appearing",
    "armorclass": "armor_class",
    "movement": "move",
    "hitdice": "hit_dice",
    "thac0": "thac0",
    "treasure": "treasure_type",
    "noofattacks": "no_of_attacks",
    "damageattack": "damage_attack",
    "specialattack": "special_attacks",
    "specialdefenses": "special_defenses",
    "magicalresistance": "magic_resistance",
    "intelligence": "intelligence",
    "alignment": "alignment",
    "size": "size",
    "moral": "morale",
    "xp": "xp_value",
    "terrain": "climate_terrain",
}
# DB columns the wiki infobox doesn't provide.
_NULL_COLUMNS = ("pct_in_lair", "psionic_ability", "attack_defense_modes")
_ALL_COLUMNS = (
    "id", "name", "frequency", "no_appearing", "armor_class", "move",
    "hit_dice", "thac0", "pct_in_lair", "treasure_type", "no_of_attacks",
    "damage_attack", "special_attacks", "special_defenses", "magic_resistance",
    "intelligence", "alignment", "size", "psionic_ability",
    "attack_defense_modes", "description", "climate_terrain", "morale",
    "xp_value", "categories", "source",
)


# --------------------------------------------------------------------------- #
# MediaWiki API
# --------------------------------------------------------------------------- #
def _api_get(params: dict, retries: int = 4) -> dict:
    params = {**params, "format": "json", "maxlag": "5"}
    url = API + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API failed after {retries} tries: {last}")


def iter_category_members(category: str, sleep: float = 0.2):
    cont = None
    while True:
        params = {
            "action": "query", "list": "categorymembers",
            "cmtitle": f"Category:{category}", "cmlimit": "500", "cmtype": "page",
        }
        if cont:
            params["cmcontinue"] = cont
        data = _api_get(params)
        for m in data.get("query", {}).get("categorymembers", []):
            yield m["title"]
        cont = data.get("continue", {}).get("cmcontinue")
        if not cont:
            break
        time.sleep(sleep)


def fetch_wikitext_batch(titles: list[str], sleep: float = 0.2) -> dict[str, str]:
    out: dict[str, str] = {}
    for i in range(0, len(titles), 40):
        data = _api_get({
            "action": "query", "prop": "revisions", "rvprop": "content",
            "rvslots": "main", "titles": "|".join(titles[i:i + 40]),
        })
        query = data.get("query", {})
        norm = {n["to"]: n["from"] for n in query.get("normalized", [])}
        for page in query.get("pages", {}).values():
            title = page.get("title", "")
            revs = page.get("revisions")
            if revs:
                out[norm.get(title, title)] = \
                    revs[0].get("slots", {}).get("main", {}).get("*", "")
        time.sleep(sleep)
    return out


def fetch_image_urls(filenames: list[str], sleep: float = 0.2) -> dict[str, str]:
    """Resolve {wiki image filename -> direct download URL} via imageinfo."""
    out: dict[str, str] = {}
    uniq = sorted({f for f in filenames if f})
    for i in range(0, len(uniq), 40):
        chunk = uniq[i:i + 40]
        data = _api_get({
            "action": "query", "prop": "imageinfo", "iiprop": "url",
            "titles": "|".join(f"File:{f}" for f in chunk),
        })
        query = data.get("query", {})
        norm = {n["to"]: n["from"] for n in query.get("normalized", [])}
        for page in query.get("pages", {}).values():
            title = page.get("title", "")           # e.g. "File:Aboleth 2e.png"
            ii = page.get("imageinfo")
            if not ii:
                continue
            orig = norm.get(title, title)
            fname = orig.split(":", 1)[1] if ":" in orig else orig
            out[fname] = ii[0]["url"]
        time.sleep(sleep)
    return out


def download_image(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = resp.read()
        if not data:
            return False
        dest.write_bytes(data)
        return True
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Wikitext parsing (shared style with import_wiki_magic_items.py)
# --------------------------------------------------------------------------- #
def _split_top_level_pipes(s: str) -> list[str]:
    parts, dsq, dbr, buf, i = [], 0, 0, [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two == "[[":
            dsq += 1; buf.append(two); i += 2; continue
        if two == "]]":
            dsq = max(0, dsq - 1); buf.append(two); i += 2; continue
        if two == "{{":
            dbr += 1; buf.append(two); i += 2; continue
        if two == "}}":
            dbr = max(0, dbr - 1); buf.append(two); i += 2; continue
        c = s[i]
        if c == "|" and dsq == 0 and dbr == 0:
            parts.append("".join(buf)); buf = []
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _find_template(wikitext: str, name: str) -> tuple[str | None, int]:
    m = re.search(r"\{\{\s*" + name + r"\b", wikitext, re.IGNORECASE)
    if not m:
        return None, 0
    start, depth, i = m.start(), 0, m.start()
    while i < len(wikitext):
        two = wikitext[i:i + 2]
        if two == "{{":
            depth += 1; i += 2
        elif two == "}}":
            depth -= 1; i += 2
            if depth == 0:
                return wikitext[start + 2:i - 2], i
        else:
            i += 1
    return None, 0


def parse_infobox(inner: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for seg in _split_top_level_pipes(inner)[1:]:
        if "=" in seg:
            k, _, v = seg.partition("=")
            fields[k.strip().lower()] = v.strip()
    return fields


def clean_markup(text: str) -> str:
    if not text:
        return ""
    t = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    t = re.sub(r"<ref[^>]*>.*?</ref>", "", t, flags=re.DOTALL)
    t = re.sub(r"<ref[^>]*/>", "", t)
    t = re.sub(r"\{\{\s*br\s*\}\}", "\n", t, flags=re.IGNORECASE)
    t = re.sub(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]+)\]\]", r"\1", t)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"\{\{[^{}]*\}\}", "", t)
    t = re.sub(r"'''([^']+)'''", r"\1", t)
    t = re.sub(r"''([^']+)''", r"\1", t)
    t = re.sub(r"^=+\s*(.*?)\s*=+\s*$", r"\1", t, flags=re.MULTILINE)
    t = re.sub(r"<[^>]+>", "", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def monster_slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def split_metadata(description: str) -> tuple[str, list[str], str]:
    """Split a wiki-imported description into (body, categories, source).

    Imported descriptions carry a trailing metadata block: zero or more
    ``Category:<name>`` lines followed by a (possibly multi-line) ``Source: ...``
    footer ending in the wiki domain tag. This peels that block off the end:

    - ``source``     — text after ``Source:`` with newlines collapsed to spaces
                       ('' if no footer).
    - ``categories`` — de-duplicated category names with the ``Category:`` prefix
                       stripped, in document order ([] if none).
    - ``body``       — the description with the metadata block removed.

    Only the *trailing* contiguous run of category lines is treated as metadata,
    so stray ``Category:`` lines embedded mid-body are left untouched.
    """
    if not description:
        return (description or "", [], "")
    lines = description.split("\n")

    # 1. Pull off the Source: footer — the last 'Source:' line to end-of-text
    #    (the source string may wrap across several following lines).
    source = ""
    src_idx = None
    for i, ln in enumerate(lines):
        if ln.startswith("Source:"):
            src_idx = i
    if src_idx is not None:
        parts = [lines[src_idx][len("Source:"):].strip()]
        parts += [l.strip() for l in lines[src_idx + 1:]]
        source = " ".join(p for p in parts if p).strip()
        lines = lines[:src_idx]

    # 2. Strip the trailing contiguous run of Category:/blank lines.
    cats_rev: list[str] = []
    while lines:
        s = lines[-1].strip()
        if not s:
            lines.pop()
            continue
        if s.startswith("Category:"):
            cats_rev.append(s[len("Category:"):].strip())
            lines.pop()
            continue
        break

    seen: set[str] = set()
    categories: list[str] = []
    for c in reversed(cats_rev):
        if c and c not in seen:
            seen.add(c)
            categories.append(c)

    body = "\n".join(lines).rstrip()
    return body, categories, source


def _parse_thac0(v: str):
    m = re.search(r"\d+", v or "")
    return int(m.group(0)) if m else None


def build_row(title: str, wikitext: str) -> dict | None:
    """Map a wiki page to a monsters-row dict (+ '_image' filename), or None to
    skip (redirect / not a creature infobox / no content)."""
    if re.match(r"\s*#redirect", wikitext, re.IGNORECASE):
        return None
    inner, end = _find_template(wikitext, "Creature")
    if inner is None:
        return None
    fields = parse_infobox(inner)

    name = (clean_markup(fields.get("name", "")) or title).strip().lower()
    if not name:
        return None
    # Skip overview/index pages ("X, general information") — not real stat blocks.
    if ("general information" in name or name.endswith(", general")
            or name.endswith(" (general)") or "overview" in name):
        return None

    row = {c: None for c in _ALL_COLUMNS}
    for wiki_key, col in _FIELD_MAP.items():
        if wiki_key in fields:
            val = clean_markup(fields[wiki_key])
            row[col] = _parse_thac0(val) if col == "thac0" else (val or None)

    raw_body = clean_markup(wikitext[end:] if end else wikitext)
    body, categories, _ = split_metadata(raw_body)
    src = clean_markup(fields.get("source", ""))
    row["description"] = body or None
    row["categories"] = json.dumps(categories) if categories else None
    row["source"] = f"{src} ({_SOURCE_TAG})" if src else _SOURCE_TAG
    row["name"] = name

    img = fields.get("image", "").strip()
    img = re.sub(r"\[\[(?:File|Image):", "", img, flags=re.IGNORECASE).strip("[]| ")
    img = img.split("|")[0].strip() if img else ""
    row["_image"] = img or None
    return row


# --------------------------------------------------------------------------- #
# Multi-creature pages
# --------------------------------------------------------------------------- #
# A page that covers a family ("Bat", "Baboon") carries its stat blocks in one
# of two shapes the single-infobox reader above never saw:
#
#   1. numbered infobox fields — ``name1 = Wild Baboon | hitdice1 = 1+1 |
#      name2 = Banderlog | hitdice2 = 4`` — one index per creature;
#   2. an article table after an empty infobox — a header row of variant names
#      and one ``! Hit Dice: | … | …`` row per stat, one column per creature.
#
# Both produce one row per variant. The family row the old importer wrote
# (name only, no stats) is left alone as the overview entry.

# Article-table row label (lower-cased, trailing colon stripped) -> column.
_TABLE_FIELD_MAP = {
    "climate/terrain": "climate_terrain", "terrain": "climate_terrain",
    "frequency": "frequency",
    "no. appearing": "no_appearing", "number appearing": "no_appearing",
    "armor class": "armor_class", "armour class": "armor_class",
    "movement": "move",
    "hit dice": "hit_dice",
    "thac0": "thac0",
    "% in lair": "pct_in_lair",
    "treasure": "treasure_type", "treasure type": "treasure_type",
    "no. of attacks": "no_of_attacks", "number of attacks": "no_of_attacks",
    "damage/attack": "damage_attack", "damage/attacks": "damage_attack",
    "special attacks": "special_attacks",
    "special defenses": "special_defenses", "special defences": "special_defenses",
    "magic resistance": "magic_resistance", "magical resistance": "magic_resistance",
    "intelligence": "intelligence",
    "alignment": "alignment",
    "size": "size",
    "psionic ability": "psionic_ability",
    "attack/defense modes": "attack_defense_modes",
    "morale": "morale",
    "xp value": "xp_value", "xp": "xp_value",
}

_STAT_COLUMNS = tuple(dict.fromkeys(
    list(_FIELD_MAP.values()) + list(_TABLE_FIELD_MAP.values())))


def _indexed_key(key: str) -> tuple[str, str]:
    """Split an infobox key into (field, index): 'hitdice2' -> ('hitdice', '2').

    'thac0' ends in a digit that belongs to the name, so it is split by hand —
    a generic trailing-digits split would read 'thac01' as ('thac', '01')."""
    if key.startswith("thac0"):
        return "thac0", key[5:]
    m = re.fullmatch(r"(.*?[a-z])(\d*)", key)
    return (m.group(1), m.group(2)) if m else (key, "")


def variant_name(base: str, variant: str) -> str:
    """'bat' + 'Common' -> 'bat, common'; 'baboon' + 'Wild Baboon' -> 'wild baboon'.

    A variant label that already contains the family's head noun stands on its
    own; a bare qualifier is appended to the family name, the MM's own
    'bat, common' form. A '(various)' tag on the family name is dropped, so
    the Bat page's columns land on the 'bat, common' the MM rows already use."""
    base = re.sub(r"\s*\(various\)\s*$", "", (base or "").strip().lower())
    v = re.sub(r"\s+", " ", clean_markup(variant or "")).strip().lower()
    if not v:
        return base
    if not base or v == base:
        return v
    head = [w for w in re.findall(r"[a-z]+", re.sub(r"\(.*?\)", "", base))
            if len(w) > 2]
    if head and head[-1] in re.findall(r"[a-z]+", v):
        return v
    return f"{base}, {v}"


def _base_row(name: str, body: str, categories: list[str], src: str) -> dict:
    row = {c: None for c in _ALL_COLUMNS}
    row["name"] = name
    row["description"] = body or None
    row["categories"] = json.dumps(categories) if categories else None
    row["source"] = f"{src} ({_SOURCE_TAG})" if src else _SOURCE_TAG
    return row


def _set_stat(row: dict, col: str, raw: str) -> None:
    val = clean_markup(raw or "").replace("&nbsp;", " ").strip()
    val = re.sub(r"\s*\n\s*", "; ", val)
    if not val:
        return
    row[col] = _parse_thac0(val) if col == "thac0" else val


def _split_cells(line: str) -> list[str]:
    """One wikitable line ('! a !! b' / '| a || b') into its cell values,
    dropping 'style=…|' attribute prefixes and splitting stray <th> tags."""
    line = re.sub(r"<t[hd][^>]*>", "!!", line[1:])
    cells = []
    for part in re.split(r"!!|\|\|", line):
        head, sep, tail = part.partition("|")
        if sep and "[[" not in head and "{{" not in head and "=" in head:
            part = tail
        cells.append(part.replace("{{br}}", " ").strip())
    return cells


def parse_stat_tables(wikitext: str) -> list[tuple[list[str], dict[str, list[str]]]]:
    """Every ``{| … |}`` table that has a Hit Dice row, as
    (variant names from the header row, {label: [value per column]})."""
    out = []
    for m in re.finditer(r"\{\|(.*?)\n\|\}", wikitext, re.S):
        header: list[str] | None = None
        fields: dict[str, list[str]] = {}
        for chunk in re.split(r"\n\|-[^\n]*", m.group(1)):
            cells: list[tuple[str, str]] = []
            for ln in chunk.split("\n"):
                ln = ln.strip()
                if ln[:1] in ("!", "|") and not ln.startswith("|+"):
                    cells += [(ln[0], c) for c in _split_cells(ln)]
            if not cells:
                continue
            if header is None and all(t == "!" for t, _ in cells):
                header = [c for _, c in cells]
                continue
            label = clean_markup(cells[0][1]).rstrip(": ").strip().lower()
            fields[label] = [c for _, c in cells[1:]]
        if "hit dice" in fields:
            out.append((header or [], fields))
    return out


def build_rows(title: str, wikitext: str) -> list[dict]:
    """Every creature on a page: the plain single-infobox row (as build_row),
    one row per numbered infobox index, and one row per article-table column.
    Returns [] for redirects and pages with no creature infobox."""
    if re.match(r"\s*#redirect", wikitext or "", re.IGNORECASE):
        return []
    rows: list[dict] = []
    rest, seen_any = wikitext, False
    while True:
        inner, end = _find_template(rest, "Creature")
        if inner is None:
            break
        seen_any = True
        fields = parse_infobox(inner)
        tail = rest[end:]
        nxt = re.search(r"\{\{\s*Creature\b", tail, re.IGNORECASE)
        section = tail[:nxt.start()] if nxt else tail
        rest = tail

        base = (clean_markup(fields.get("name", "")) or title).strip().lower()
        body, categories, _ = split_metadata(clean_markup(section))
        # Only the text before the first stat table is description.
        body = re.split(r"\n\{\|", body)[0].strip() if body else body

        by_index: dict[str, dict[str, str]] = {}
        for k, v in fields.items():
            f, idx = _indexed_key(k)
            by_index.setdefault(idx, {})[f] = v
        numbered = sorted((i for i in by_index if i), key=int)

        if numbered:
            for i in numbered:
                f = by_index[i]
                if not any(k in f for k in _FIELD_MAP):
                    continue
                row = _base_row(variant_name(base, f.get("name", "")), body, categories,
                                clean_markup(f.get("source", "") or fields.get("source", "")))
                for wiki_key, col in _FIELD_MAP.items():
                    _set_stat(row, col, f.get(wiki_key, ""))
                rows.append(row)
        else:
            plain = build_row(title, "{{" + inner + "}}" + section)
            if plain is not None:
                plain["description"] = body or None
                rows.append(plain)

        for header, tfields in parse_stat_tables(section):
            ncols = len(tfields["hit dice"])
            names = header[1:] if len(header) > ncols else header
            for c in range(ncols):
                label = names[c] if c < len(names) else ""
                row = _base_row(variant_name(base, label) if ncols > 1 else base,
                                body, categories,
                                clean_markup(fields.get("source", "")))
                for tlabel, vals in tfields.items():
                    col = _TABLE_FIELD_MAP.get(tlabel)
                    if col and c < len(vals):
                        _set_stat(row, col, vals[c])
                rows.append(row)
    if not seen_any:
        return []
    out, seen = [], set()
    for r in rows:
        if not r["name"] or r["name"] in seen:
            continue
        if ("general information" in r["name"] or r["name"].endswith(", general")
                or r["name"].endswith(" (general)") or "overview" in r["name"]):
            continue
        seen.add(r["name"])
        out.append(r)
    return out


def has_stats(row) -> bool:
    return bool((row["hit_dice"] if row["hit_dice"] is not None else "").strip())


def name_variants(name: str) -> set[str]:
    """Spellings a creature may already be recorded under: 'bat, common',
    'common bat', 'bat (common)'."""
    out = {name}
    base, sep, var = name.partition(", ")
    if sep:
        out |= {f"{var} {base}", f"{base} ({var})"}
    return out


# --------------------------------------------------------------------------- #
# DB
# --------------------------------------------------------------------------- #
def existing_names(conn) -> set[str]:
    return {r[0].strip().lower()
            for r in conn.execute("SELECT name FROM monsters WHERE name IS NOT NULL")}


def statless_names(conn) -> set[str]:
    """Recorded creatures with no hit dice — the rows a multi-creature page
    produced before its numbered fields and stat tables were read."""
    return {r[0].strip().lower() for r in conn.execute(
        "SELECT name FROM monsters WHERE name IS NOT NULL "
        "AND coalesce(trim(hit_dice), '') = ''")}


def fill_stats(conn, row: dict) -> int:
    """Write a parsed row's stat columns into the recorded creature of the same
    name, only where that row has nothing. Description, source and categories
    are kept. Returns the number of rows updated."""
    cols = [c for c in _STAT_COLUMNS if row.get(c) not in (None, "")]
    if not cols:
        return 0
    sets = ", ".join(f"{c} = coalesce(nullif(trim({c}), ''), ?)" for c in cols)
    cur = conn.execute(
        f"UPDATE monsters SET {sets} WHERE lower(trim(name)) = ? "
        f"AND coalesce(trim(hit_dice), '') = ''",
        [row[c] for c in cols] + [row["name"]])
    return cur.rowcount


def next_id(conn) -> int:
    cur = conn.execute("SELECT MAX(id) FROM monsters").fetchone()[0]
    return max((cur or 0) + 1, _ID_BASE)


def insert_row(conn, item_id: int, row: dict) -> None:
    cols = ", ".join(_ALL_COLUMNS)
    qs = ", ".join("?" for _ in _ALL_COLUMNS)
    vals = [item_id if c == "id" else row.get(c) for c in _ALL_COLUMNS]
    conn.execute(f"INSERT INTO monsters ({cols}) VALUES ({qs})", vals)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--db", default=str(_DEFAULT_DB))
    ap.add_argument("--img-dir", default=str(_DEFAULT_IMG_DIR))
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    db_path, img_dir = Path(args.db), Path(args.img_dir)
    if not db_path.exists():
        print(f"error: monsters db not found: {db_path}", file=sys.stderr)
        return 2
    if not args.dry_run and not args.no_images:
        img_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(db_path))
    try:
        have = existing_names(conn)
        nid = next_id(conn)
        print(f"DB: {db_path} — {len(have)} creatures recorded; new ids from {nid}.")

        print("Listing Category:Creatures members…")
        titles = list(iter_category_members("Creatures"))
        if args.limit:
            titles = titles[:args.limit]
        print(f"{len(titles)} member pages to examine.")

        wikitexts: dict[str, str] = {}
        for i in range(0, len(titles), 40):
            wikitexts.update(fetch_wikitext_batch(titles[i:i + 40]))
            print(f"  fetched {min(i + 40, len(titles))}/{len(titles)} pages…",
                  end="\r", flush=True)
        print()

        # Build rows for creatures not already recorded; collect stats for
        # recorded ones that came in without any (multi-creature pages).
        statless = statless_names(conn)
        new_rows: list[dict] = []
        backfills: list[dict] = []
        seen: set[str] = set()
        skipped_existing = skipped_nodata = 0
        for title in titles:
            wt = wikitexts.get(title)
            rows = build_rows(title, wt) if wt else []
            if not rows:
                skipped_nodata += 1
                continue
            for row in rows:
                key = row["name"]
                if key in seen:
                    continue
                seen.add(key)
                if key in statless and has_stats(row):
                    backfills.append(row)
                elif name_variants(key) & have:
                    skipped_existing += 1
                elif has_stats(row) or len(rows) == 1:
                    new_rows.append(row)

        print(f"{len(new_rows)} new creatures, {len(backfills)} recorded creatures "
              f"to fill in (skipped {skipped_existing} already-recorded, "
              f"{skipped_nodata} redirects/non-creature).")
        if args.verbose:
            for row in backfills:
                print(f"  ~ {row['name']}  (HD {row['hit_dice']})")

        # Resolve image URLs in bulk.
        url_map: dict[str, str] = {}
        if not args.no_images:
            wanted = [r["_image"] for r in new_rows if r.get("_image")]
            print(f"Resolving {len(set(filter(None, wanted)))} image URLs…")
            if not args.dry_run:
                url_map = fetch_image_urls(wanted)

        inserted = images = 0
        for row in new_rows:
            if args.dry_run:
                inserted += 1
                if args.verbose:
                    print(f"  + {row['name']}  (HD {row['hit_dice']}, "
                          f"img={'yes' if row.get('_image') else 'no'})")
                continue
            insert_row(conn, nid, row)
            nid += 1
            inserted += 1
            # Download image into <slug>.<ext>.
            img = row.get("_image")
            if img and not args.no_images:
                ext = Path(img).suffix.lower()
                if ext in _IMG_EXTS and img in url_map:
                    dest = img_dir / f"{monster_slug(row['name'])}{ext}"
                    if not dest.exists() and download_image(url_map[img], dest):
                        images += 1
            if args.verbose and inserted % 50 == 0:
                print(f"  …{inserted} inserted", end="\r", flush=True)

        filled = 0
        for row in backfills:
            if not args.dry_run:
                filled += fill_stats(conn, row)
            else:
                filled += 1

        if not args.dry_run:
            conn.commit()

        verb = "would insert" if args.dry_run else "inserted"
        print(f"\nDone. {verb} {inserted} creatures, filled in {filled}; "
              f"downloaded {images} images.")
        if args.dry_run:
            print("(dry run — nothing written)")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
