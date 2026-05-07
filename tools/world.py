"""World-building tools: NPCs, locations, and area management."""
import json
import random
import re
import sqlite3
from pathlib import Path
import _campaign as _c
import names

BASE_DIR = Path(__file__).parent.parent
_2E_DB   = BASE_DIR / "global" / "2e.db"

# (path, mtime) → first-heading. Avoids re-reading every NPC/location markdown
# on every list_characters / list_locations call.
_heading_cache: dict[str, tuple[float, str]] = {}


def _first_heading(path: Path) -> str:
    """Return the first markdown heading text, cached by file mtime."""
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return path.stem
    cached = _heading_cache.get(key)
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    if not st.st_size:
        name = path.stem
    else:
        with path.open(encoding="utf-8") as f:
            first_line = f.readline()
        name = first_line.lstrip("#").strip() or path.stem
    _heading_cache[key] = (st.st_mtime, name)
    return name


def _roll_hd(notation: str) -> int:
    """Roll a hit-dice expression like '1d8', '3d10+1'."""
    m = re.match(r'^(\d+)d(\d+)([+-]\d+)?$', notation.strip().lower())
    if not m:
        return 4
    n, sides, mod = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return max(1, sum(random.randint(1, sides) for _ in range(n)) + mod)


_DEFAULT_AC = {
    "fighter": 5, "ranger": 6, "paladin": 4, "cleric": 5, "druid": 7,
    "thief": 8, "bard": 7, "mage": 10, "wizard": 10, "illusionist": 10,
}
_DEFAULT_WEAPON = {
    "fighter": ("long sword", "1d8", 5),
    "paladin": ("long sword", "1d8", 5),
    "ranger":  ("long sword", "1d8", 5),
    "cleric":  ("mace", "1d6+1", 7),
    "druid":   ("club", "1d6", 4),
    "thief":   ("short sword", "1d6", 3),
    "bard":    ("short sword", "1d6", 3),
    "mage":    ("dagger", "1d4", 2),
    "wizard":  ("dagger", "1d4", 2),
    "illusionist": ("dagger", "1d4", 2),
}
_RACE_ADJ = {
    "dwarf":    {"con": 1, "cha": -1},
    "elf":      {"dex": 1, "con": -1},
    "halfling": {"dex": 1, "str": -1},
    "gnome":    {"int": 1, "wis": -1},
    "halfelf":  {},
    "halforc":  {"str": 1, "con": 1, "cha": -2},
    "human":    {},
}


def register(mcp):

    @mcp.tool()
    def introduce_npc(race: str, gender: str = "", description: str = "") -> dict:
        """Generate a named NPC, create their character file, and record the encounter.
        race: human/halfling/elf/dwarf/gnome/halfelf/halforc
        gender: 'male', 'female', or empty for random
        description: brief description or role (e.g. 'innkeeper', 'guard captain')
        Automatically generates a portrait (best-effort).
        For stat blocks use set_npc_stats(slug, ...) after introduction."""
        cfg = _c.load_campaign()
        female = gender.lower() == "female"
        name = names.generate(race=race, female=female)

        base_slug = re.sub(r"[^a-z0-9]", "", name.lower().split()[0])
        chars_dir = cfg["_data_dir"] / "characters"
        chars_dir.mkdir(parents=True, exist_ok=True)

        slug = base_slug
        counter = 2
        while (chars_dir / f"{slug}.md").exists():
            slug = f"{base_slug}{counter}"
            counter += 1

        race_line = race.capitalize()
        if description:
            race_line += f" — {description}"

        content = f"# {name}\n*{race_line}*\n\n## Status\nAlive\n\n## Notes\n\n"
        (chars_dir / f"{slug}.md").write_text(content, encoding="utf-8")

        _c.append_event(cfg, {
            "type":  "npc_met",
            "slug":  slug,
            "name":  name,
            "race":  race,
            "notes": description,
        })

        # Auto-generate portrait — best-effort
        try:
            from tools.images import generate_portrait_for
            portrait_prompt = f"Portrait of a {gender or 'person'} {race} {description}".strip()
            generate_portrait_for(cfg, slug, portrait_prompt)
        except Exception:
            pass

        return {
            "name":  name,
            "slug":  slug,
            "race":  race,
            "file":  str(chars_dir / f"{slug}.md"),
        }

    @mcp.tool()
    def set_npc_stats(
        slug: str,
        cls: str = "",
        level: int = 0,
        hp: int = 0,
        ac: int = 0,
        thac0: int = 0,
        saves: list = None,
        attacks: list = None,
        ability_scores: dict = None,
        weapon_profs: list = None,
        nwps: list = None,
        natural_abilities: list = None,
        inventory: list = None,
        background: str = "",
        portrait_prompt: str = "",
    ) -> dict:
        """Attach a full stat block to a named NPC (stored in campaign.json['npcs']).
        Regenerates the character sheet markdown automatically.
        slug: must match an existing characters/<slug>.md file
        attacks: [{name, speed, attacks, thac0, damage_sm, damage_l?, range?}, ...]
        portrait_prompt: canonical image-generation prompt stored for future reuse
        """
        cfg = _c.load_campaign()
        chars_dir = cfg["_data_dir"] / "characters"
        md_path = chars_dir / f"{slug}.md"
        if not md_path.exists():
            return {"error": f"No character file for '{slug}'. Call introduce_npc first."}

        # Read name from existing file
        first_line = md_path.read_text(encoding="utf-8").splitlines()[0]
        label = first_line.lstrip("#").strip()

        # Read existing NPC data or start fresh
        npc_data = cfg.setdefault("npcs", {}).get(slug, {})
        npc_data["label"] = label
        npc_data["slug"]  = slug

        if cls:             npc_data["cls"]              = cls
        if level:           npc_data["level"]            = level
        if hp:              npc_data["hp_max"]           = hp
        if ac:              npc_data["ac"]               = ac
        if thac0:           npc_data["thac0"]            = thac0
        if saves:           npc_data["saves"]            = saves
        if attacks:         npc_data["attacks"]          = attacks
        if ability_scores:  npc_data["ability_scores"]   = ability_scores
        if weapon_profs:    npc_data["weapon_profs"]     = weapon_profs
        if nwps:            npc_data["nwps"]             = nwps
        if natural_abilities: npc_data["natural_abilities"] = natural_abilities
        if inventory:       npc_data["inventory"]        = inventory
        if background:          npc_data["background"]       = background
        if portrait_prompt:     npc_data["portrait_prompt"]  = portrait_prompt

        cfg["npcs"][slug] = npc_data
        _c.save_campaign(cfg)
        _c.write_character_sheet(cfg, slug, npc_data)

        return {"slug": slug, "label": label, "updated": True}

    @mcp.tool()
    def quick_npc(
        cls:          str,
        level:        int = 1,
        race:         str = "human",
        gender:       str = "",
        description:  str = "",
        ac:           int = 0,
        weapon:       str = "",
    ) -> dict:
        """Generate a fully-statted NPC in one call. Synthesises stats from
        class_lookup (THAC0, saves, spell slots, HD) and applies sensible
        defaults for AC and weapon based on class. Use for incidental NPCs
        (guards, hirelings, bandits) instead of inventing numbers.

        cls:         class name (Fighter, Mage, Cleric, Thief, ...)
        level:       class level (1+)
        race:        human / elf / dwarf / halfling / gnome / halfelf / halforc
        gender:      'male' / 'female' / blank for random
        description: role/notes appended to the character file (e.g. 'town guard')
        ac:          override the class-default AC
        weapon:      override the class-default weapon ('long sword', etc.)

        The NPC gets a generated name, character file, stat block in
        campaign.json[npcs], and a portrait (best-effort)."""
        if not _2E_DB.exists():
            return {"error": "2e.db not found. Run tools/build_2e_db.py first."}

        cfg = _c.load_campaign()
        female = gender.lower() == "female"
        npc_name = names.generate(race=race, female=female)

        base_slug = re.sub(r"[^a-z0-9]", "", npc_name.lower().split()[0])
        chars_dir = cfg["_data_dir"] / "characters"
        chars_dir.mkdir(parents=True, exist_ok=True)
        slug = base_slug
        ctr = 2
        while (chars_dir / f"{slug}.md").exists():
            slug = f"{base_slug}{ctr}"
            ctr += 1

        # ── Pull class data ──────────────────────────────────────────────
        conn = sqlite3.connect(str(_2E_DB))
        conn.row_factory = sqlite3.Row
        try:
            cls_row = conn.execute(
                "SELECT id, name, hit_die FROM classes WHERE name LIKE ? COLLATE NOCASE LIMIT 1",
                (f"%{cls}%",),
            ).fetchone()
            if cls_row is None:
                return {"error": f"Class '{cls}' not found in 2e.db."}
            class_id = cls_row["id"]
            class_name = cls_row["name"]
            hit_die = cls_row["hit_die"] or "1d8"

            lvl_row = conn.execute(
                "SELECT thac0, save_paralysis, save_rsw, save_petrify, save_breath, save_spell, "
                "spell_slots, hit_dice FROM class_levels WHERE class_id=? AND level=?",
                (class_id, level),
            ).fetchone()
            if lvl_row is None:
                return {"error": f"No level-{level} data for {class_name}."}
            thac0 = lvl_row["thac0"]
            saves = [lvl_row["save_paralysis"], lvl_row["save_rsw"], lvl_row["save_petrify"],
                     lvl_row["save_breath"], lvl_row["save_spell"]]
            spell_slots = json.loads(lvl_row["spell_slots"]) if lvl_row["spell_slots"] else None
            hd_notation = lvl_row["hit_dice"] or hit_die
        finally:
            conn.close()

        # ── Roll HP ──────────────────────────────────────────────────────
        # NPCs get average rolls (avoid extreme outliers)
        hp_max = max(level, _roll_hd(hd_notation))

        # ── Defaults ─────────────────────────────────────────────────────
        cls_lower = class_name.lower()
        kw = next((k for k in _DEFAULT_AC if k in cls_lower), "fighter")
        default_ac = _DEFAULT_AC.get(kw, 8)
        weapon_name, weapon_dmg, weapon_speed = _DEFAULT_WEAPON.get(kw, ("club", "1d6", 4))

        final_ac = ac if ac else default_ac
        final_weapon = weapon if weapon else weapon_name

        # ── Stat block ───────────────────────────────────────────────────
        npc_data = {
            "label":        npc_name,
            "slug":         slug,
            "cls":          class_name,
            "race":         race,
            "gender":       gender or "male",
            "level":        level,
            "hp_max":       hp_max,
            "ac":           final_ac,
            "thac0":        thac0,
            "saves":        saves,
            "weapon":       final_weapon,
            "weapon_speed": weapon_speed,
            "weapon_dmg":   weapon_dmg,
            "bonus_hit":    0,
            "bonus_dmg":    0,
            "background":   description,
        }
        if spell_slots:
            npc_data["spell_slots"] = {str(k): int(v) for k, v in spell_slots.items()}

        # ── Persist ──────────────────────────────────────────────────────
        race_line = race.capitalize()
        if description:
            race_line += f" — {description}"
        content = f"# {npc_name}\n*{race_line}*  \n**{class_name} (Level {level})**\n\n## Status\nAlive\n\n## Notes\n\n"
        (chars_dir / f"{slug}.md").write_text(content, encoding="utf-8")

        cfg.setdefault("npcs", {})[slug] = npc_data
        _c.save_campaign(cfg)
        _c.write_character_sheet(cfg, slug, npc_data)

        _c.append_event(cfg, {
            "type":  "npc_met",
            "slug":  slug,
            "name":  npc_name,
            "race":  race,
            "notes": f"{class_name} L{level} — {description}".strip(),
        })

        # Best-effort portrait
        try:
            from tools.images import generate_portrait_for
            portrait_prompt = f"{gender or 'person'} {race} {class_name.lower()} {description}".strip()
            generate_portrait_for(cfg, slug, portrait_prompt)
        except Exception:
            pass

        return {
            "name":         npc_name,
            "slug":         slug,
            "class":        class_name,
            "level":        level,
            "hp_max":       hp_max,
            "ac":           final_ac,
            "thac0":        thac0,
            "saves":        saves,
            "weapon":       final_weapon,
            "spell_slots":  spell_slots,
        }

    @mcp.tool()
    def refresh_sheet(key: str) -> dict:
        """Regenerate the markdown sheet for a PC or NPC from stored campaign.json data.
        key: character key (from 'characters' or 'npcs' in campaign.json)
        Use this after manually editing campaign.json or after a stat change."""
        cfg = _c.load_campaign()
        char = cfg.get("characters", {}).get(key) or cfg.get("npcs", {}).get(key)
        if not char:
            return {
                "error": (
                    f"No stored stats for '{key}'. "
                    "PCs use add_character; NPCs use set_npc_stats first."
                )
            }
        _c.write_character_sheet(cfg, key, char)
        return {"key": key, "label": char.get("label", key), "refreshed": True}

    @mcp.tool()
    def set_disposition(slug: str, value: int, reason: str = "", faction: str = "") -> dict:
        """Set or adjust a named NPC's persistent disposition toward the party.
        Range: -100 (sworn enemy) to +100 (devoted ally). 0 = neutral.
        Disposition feeds into reaction(npc=slug) automatically (disposition/20
        becomes the modifier, so ±100 = ±5 to the 2d10 roll).

        Use after meaningful interactions:
        - +20 to +40 for significant favours, gifts, helpful actions
        - -20 to -40 for slights, broken promises, theft witnessed
        - ±60+ for life-changing events (saved their child, killed their kin)

        reason:  one-line note logged to the NPC's history
        faction: optional faction slug — links the NPC to a faction so reaction()
                 also picks up the faction-wide reputation."""
        cfg = _c.load_campaign()
        slug = slug.lower().strip()
        npcs = cfg.setdefault("npcs", {})
        npc = npcs.get(slug)
        if not npc:
            # Allow setting on an NPC that has only a markdown file but no stat block
            md_path = cfg["_data_dir"] / "characters" / f"{slug}.md"
            if not md_path.exists():
                return {"error": f"No NPC '{slug}' found (no stat block, no character file)."}
            label = md_path.read_text(encoding="utf-8").splitlines()[0].lstrip("#").strip() or slug
            npcs[slug] = {"label": label, "slug": slug}
            npc = npcs[slug]

        before = int(npc.get("disposition", 0))
        npc["disposition"] = max(-100, min(100, int(value)))
        if faction:
            npc["faction"] = faction.lower().strip()
        _c.save_campaign(cfg)

        _c.append_event(cfg, {
            "type":   "disposition_change",
            "slug":   slug,
            "before": before,
            "after":  npc["disposition"],
            "notes":  reason or f"disposition {before:+d} → {npc['disposition']:+d}",
        })

        return {
            "slug":       slug,
            "label":      npc.get("label", slug),
            "before":     before,
            "after":      npc["disposition"],
            "faction":    npc.get("faction", ""),
            "modifier_applied_to_reaction": npc["disposition"] // 20,
        }

    @mcp.tool()
    def get_character(slug: str) -> dict:
        """Read a character file by slug.
        Returns the full markdown content, or an error if not found."""
        cfg = _c.load_campaign()
        path = cfg["_data_dir"] / "characters" / f"{slug}.md"
        if not path.exists():
            return {"error": f"Character '{slug}' not found at {path}"}
        return {"slug": slug, "content": path.read_text(encoding="utf-8")}

    @mcp.tool()
    def update_character(slug: str, content: str) -> dict:
        """Overwrite a character file with new content and log the update.
        slug: character slug (filename without .md)
        content: full new markdown content"""
        cfg = _c.load_campaign()
        path = cfg["_data_dir"] / "characters" / f"{slug}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        _c.append_event(cfg, {
            "type":  "npc_interaction",
            "slug":  slug,
            "notes": "Character file updated",
        })

        return {"slug": slug, "updated": True}

    @mcp.tool()
    def list_characters() -> dict:
        """List all character files in the current campaign's characters/ directory.
        Returns slug and name (first heading) for each."""
        cfg = _c.load_campaign()
        chars_dir = cfg["_data_dir"] / "characters"
        if not chars_dir.exists():
            return {"characters": []}

        characters = []
        for md in sorted(chars_dir.glob("*.md")):
            characters.append({"slug": md.stem, "name": _first_heading(md)})

        return {"characters": characters}

    @mcp.tool()
    def create_area(slug: str, name: str, content: str) -> dict:
        """Create a top-level area (city, region, dungeon complex) and its subdirectory.
        slug: filesystem slug (e.g. 'phlan', 'dungeon-of-doom')
        name: display name (e.g. 'Phlan')
        content: initial markdown body"""
        cfg = _c.load_campaign()
        locs_dir = cfg["_data_dir"] / "locations"
        locs_dir.mkdir(parents=True, exist_ok=True)

        area_file = locs_dir / f"{slug}.md"
        if not content.lstrip().startswith("#"):
            content = f"# {name}\n\n{content}"
        area_file.write_text(content, encoding="utf-8")

        # Create the subdirectory for places within this area
        (locs_dir / slug).mkdir(parents=True, exist_ok=True)

        _c.append_event(cfg, {
            "type": "location_visited",
            "slug": slug,
            "area": "",
            "name": name,
        })

        return {"slug": slug, "path": str(area_file)}

    @mcp.tool()
    def create_location(slug: str, name: str, content: str, area: str = "") -> dict:
        """Create a location file (a place within an area, or a standalone location).
        slug: filesystem slug (e.g. 'the-sorcerers-lair')
        name: display name
        content: initial markdown body
        area: parent area slug, or empty for a top-level location"""
        cfg = _c.load_campaign()
        locs_dir = cfg["_data_dir"] / "locations"

        if area:
            dest_dir = locs_dir / area
        else:
            dest_dir = locs_dir

        dest_dir.mkdir(parents=True, exist_ok=True)

        if not content.lstrip().startswith("#"):
            content = f"# {name}\n\n{content}"

        loc_file = dest_dir / f"{slug}.md"
        loc_file.write_text(content, encoding="utf-8")

        _c.append_event(cfg, {
            "type": "location_visited",
            "slug": slug,
            "area": area,
            "name": name,
        })

        return {"slug": slug, "area": area, "path": str(loc_file)}

    @mcp.tool()
    def update_location(slug: str, content: str, area: str = "") -> dict:
        """Overwrite a location file with new content.
        slug: location slug
        content: full new markdown content
        area: parent area slug, or empty for top-level"""
        cfg = _c.load_campaign()
        locs_dir = cfg["_data_dir"] / "locations"

        if area:
            path = locs_dir / area / f"{slug}.md"
        else:
            path = locs_dir / f"{slug}.md"

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

        return {"slug": slug, "area": area, "updated": True}

    @mcp.tool()
    def get_location(slug: str, area: str = "") -> dict:
        """Read a location file by slug (and optional area).
        Returns full markdown content, or an error if not found."""
        cfg = _c.load_campaign()
        locs_dir = cfg["_data_dir"] / "locations"

        if area:
            path = locs_dir / area / f"{slug}.md"
        else:
            path = locs_dir / f"{slug}.md"

        if not path.exists():
            return {"error": f"Location '{slug}' (area='{area}') not found at {path}"}

        return {"slug": slug, "area": area, "content": path.read_text(encoding="utf-8")}

    @mcp.tool()
    def list_locations(area: str = "") -> dict:
        """List locations in the campaign.
        If area is given, list places within that area.
        If no area, list top-level location files and available sub-areas.
        Returns: areas (list of subdirectory names) and locations (slug/area/name)."""
        cfg = _c.load_campaign()
        locs_dir = cfg["_data_dir"] / "locations"

        if not locs_dir.exists():
            return {"areas": [], "locations": []}

        areas = []
        locations = []

        if area:
            area_dir = locs_dir / area
            if not area_dir.exists():
                return {"areas": [], "locations": []}
            for md in sorted(area_dir.glob("*.md")):
                locations.append({"slug": md.stem, "area": area, "name": _first_heading(md)})
        else:
            # Top-level: .md files (not in subdirs) + list subdirs as areas
            for item in sorted(locs_dir.iterdir()):
                if item.is_dir():
                    areas.append(item.name)
                elif item.is_file() and item.suffix == ".md":
                    locations.append({"slug": item.stem, "area": "", "name": _first_heading(item)})

        return {"areas": areas, "locations": locations}
