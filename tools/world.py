"""World-building tools: NPCs, locations, and area management."""
import re
from pathlib import Path
import _campaign as _c
import names


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
            from images import generate_portrait_for
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
            slug = md.stem
            first_line = md.read_text(encoding="utf-8").splitlines()[0] if md.stat().st_size else ""
            name = first_line.lstrip("#").strip() or slug
            characters.append({"slug": slug, "name": name})

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
                slug = md.stem
                first_line = md.read_text(encoding="utf-8").splitlines()[0] if md.stat().st_size else ""
                name = first_line.lstrip("#").strip() or slug
                locations.append({"slug": slug, "area": area, "name": name})
        else:
            # Top-level: .md files (not in subdirs) + list subdirs as areas
            for item in sorted(locs_dir.iterdir()):
                if item.is_dir():
                    areas.append(item.name)
                elif item.is_file() and item.suffix == ".md":
                    slug = item.stem
                    first_line = item.read_text(encoding="utf-8").splitlines()[0] if item.stat().st_size else ""
                    name = first_line.lstrip("#").strip() or slug
                    locations.append({"slug": slug, "area": "", "name": name})

        return {"areas": areas, "locations": locations}
