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


_STAT_LABEL = {"ac": "AC", "thac0": "THAC0", "hp_max": "HP max",
               "level": "level", "movement_rate": "MV"}


def _sync_combatant(key: str, char: dict, changes: dict) -> bool:
    """Push a PC's changed stats onto their combatant in a running fight.
    Returns True when a combatant was updated. Never raises: a tracker hiccup
    must not undo a sheet change that has already been saved."""
    try:
        from tools import combat as _combat
        sess = _combat.get_session()
        if not sess:
            return False
        c = next((c for c in sess["combatants"] if c.get("_key") == key), None)
        if c is None:
            return False
        if "ac" in changes:
            c["ac"] = char["ac"]
        if "hp_max" in changes:
            c["hp_max"] = char["hp_max"]
            c["hp"] = min(c["hp"], char["hp_max"])
        if "attacks" in changes:
            blocks, _ = _combat._normalize_attacks(char)
            c["attack_blocks"] = blocks
            if blocks:
                active = next((b for b in blocks if b["name"] == c.get("weapon")), blocks[0])
                _combat._apply_attack_block(c, active)
        elif "thac0" in changes and not c.get("attack_blocks"):
            # With attack blocks, THAC0 comes from the block, not the sheet.
            c["thac0"] = char["thac0"]
        _combat._persist_session()
        return True
    except Exception:
        return False


_ABILITY_ORDER = ["str", "dex", "con", "int", "wis", "cha"]
_SAVE_SHORT = {
    "paralysis_poison_death": "PPD",
    "rod_staff_wand":         "RSW",
    "petrify_polymorph":      "PetP",
    "breath_weapon":          "Breath",
    "spell":                  "Spell",
}


def _canonical_mechanics(cfg: dict, slug: str) -> dict | None:
    """Return the canonical char dict from campaign.json for `slug`, or None.

    Checks the PC roster first, then the npcs table. campaign.json is the
    source of truth for ability scores, level, HP, AC, THAC0 and saves —
    the prose .md sheet may omit or lag them."""
    for table in ("characters", "npcs"):
        entry = (cfg.get(table) or {}).get(slug)
        if isinstance(entry, dict):
            return entry
    return None


def _mechanics_block(char: dict) -> str:
    """Render a compact, authoritative mechanics header from a char dict.

    Surfaces exactly the numbers a DM tends to archetype-guess when the prose
    sheet is silent (ability scores, esp. CON for level-up HP). Empty string
    if the dict carries nothing useful."""
    ab = char.get("ability_scores") or char.get("abilities") or {}
    cls   = char.get("cls") or char.get("class") or ""
    level = char.get("level")
    if not ab and level is None:
        return ""

    race = str(char.get("race") or "").strip()
    ident = " · ".join(p for p in [
        f"{race[:1].upper()}{race[1:]} {cls}".strip() if race else cls,
        char.get("alignment") or "",
        f"Level {level}" if level is not None else "",
        f"HP {char['hp_max']}" if char.get("hp_max") is not None else "",
        f"AC {char['ac']}"     if char.get("ac")     is not None else "",
        f"THAC0 {char['thac0']}" if char.get("thac0") is not None else "",
    ] if p)

    score_parts = []
    for a in _ABILITY_ORDER:
        if a not in ab:
            continue
        val = ab[a]
        if a == "str" and ab.get("str_pct"):
            val = f"{val}/{ab['str_pct']:02d}"
        score_parts.append(f"{a.upper()} {val}")

    lines = ["> **Canonical mechanics** — live from `campaign.json`, authoritative; "
             "prose below may lag. Never archetype-guess these."]
    if ident:
        lines.append(f"> {ident}")
    if score_parts:
        lines.append("> " + " · ".join(score_parts))
    saves = char.get("saves") or []
    if isinstance(saves, list) and saves:
        sv = " · ".join(
            f"{_SAVE_SHORT.get(s['type'], s['type'])} {s['value']}"
            for s in saves if isinstance(s, dict) and "value" in s
        )
        if sv:
            lines.append(f"> Saves — {sv}")
    return "\n".join(lines)


def _roll_hd(notation: str) -> int:
    """Roll a hit-dice expression like '1d8', '3d10+1'."""
    m = re.match(r'^(\d+)d(\d+)([+-]\d+)?$', notation.strip().lower())
    if not m:
        return 4
    n, sides, mod = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return max(1, sum(random.randint(1, sides) for _ in range(n)) + mod)


# Cap for get_character(brief=True) — the opening section, not the sheet.
_BRIEF_CHARS = 900

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

        # Auto-tag with the party's current location + chapter so the
        # /characters filter has a facet without manual bookkeeping. Both are
        # editable later via set_character_location. Best-effort.
        try:
            state = _c.load_state(cfg)
            from tools.position import current_position, position_label
            loc_name = position_label(current_position(cfg, state))
            _c.set_character_meta(
                cfg, slug,
                location=loc_name,
                chapter=f"Session {state.get('current_session', 1)}",
            )
        except Exception:
            pass

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
        level: int | None = None,
        hp: int | None = None,
        ac: int | None = None,
        thac0: int | None = None,
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
        # `is not None`, not truthiness: AC 0 and negative AC are real values.
        if level is not None: npc_data["level"]          = level
        if hp is not None:  npc_data["hp_max"]           = hp
        if ac is not None:  npc_data["ac"]               = ac
        if thac0 is not None: npc_data["thac0"]          = thac0
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
    def update_stats(
        key: str,
        ac: int | None = None,
        thac0: int | None = None,
        hp_max: int | None = None,
        level: int | None = None,
        saves: list = None,
        attacks: list = None,
        ability_scores: dict = None,
        inventory: list = None,
        weapon_profs: list = None,
        nwps: list = None,
        natural_abilities: list = None,
        movement_rate: int | None = None,
        alignment: str = "",
        reason: str = "",
    ) -> dict:
        """Change a PC's or NPC's stored mechanics in place: AC after new armour,
        THAC0/HP/saves at level-up, a rewritten attacks[] after a new weapon.
        Only the fields you pass change; everything else on the sheet is kept.

        key:    PC key or label (prefix match), else an NPC slug that already
                has a stat block (NPCs without one use set_npc_stats first)
        saves:  five ints in PP&D/RSW/PETR/BREATH/SPELL order, or [{type, value}]
        attacks / inventory / lists: replace the whole list — pass the full new list
        reason: one line for the chronicle ("plate mail and shield")

        Lowering hp_max clamps current HP to it; raising it leaves current HP
        alone (heal separately). If a fight is running, the PC's combatant
        picks up the new AC, THAC0, HP max and attack blocks immediately.
        Logs a `stats_update` event with each field's old and new value."""
        cfg = _c.load_campaign()
        pc_key, record = _c.char_key_for(cfg, key)
        kind = "pc"
        if record is None:
            slug = key.lower().strip()
            record = cfg.get("npcs", {}).get(slug)
            pc_key, kind = slug, "npc"
            if record is None:
                if (cfg["_data_dir"] / "characters" / f"{slug}.md").exists():
                    return {"error": f"NPC '{slug}' has no stat block yet — use set_npc_stats."}
                return {"error": f"No PC or NPC '{key}' found."}

        wanted: dict = {
            "ac": ac, "thac0": thac0, "hp_max": hp_max, "level": level,
            "attacks": attacks, "ability_scores": ability_scores,
            "inventory": inventory, "weapon_profs": weapon_profs, "nwps": nwps,
            "natural_abilities": natural_abilities, "movement_rate": movement_rate,
            "alignment": alignment or None,
        }
        if saves is not None:
            norm = _c.normalize_saves(saves)
            if not norm:
                return {"error": "saves must be five ints or a list of {type, value}."}
            wanted["saves"] = norm
        wanted = {k: v for k, v in wanted.items() if v is not None}
        if not wanted:
            return {"error": "Nothing to update — pass at least one stat."}

        changes = {f: [record.get(f), v] for f, v in wanted.items() if record.get(f) != v}
        label = record.get("label", pc_key)
        out: dict = {"key": pc_key, "label": label, "changes": changes,
                     "combat_synced": False}
        if not changes:
            return out

        for f, (_, new) in changes.items():
            record[f] = new
        _c.save_campaign(cfg)
        _c.write_character_sheet(cfg, pc_key, record)

        if kind == "pc" and "hp_max" in changes:
            state = _c.load_state(cfg)
            cstate = state.setdefault("characters", {}).setdefault(pc_key, {})
            cur = cstate.get("hp", changes["hp_max"][0] or record["hp_max"])
            cstate["hp"] = min(cur, record["hp_max"])
            _c.save_state(cfg, state)
            out["hp_current"] = cstate["hp"]

        if kind == "pc":
            out["combat_synced"] = _sync_combatant(pc_key, record, changes)

        if "thac0" in changes and "attacks" not in changes and any(
                isinstance(a, dict) and a.get("thac0") is not None
                for a in record.get("attacks") or []):
            out["warnings"] = ["Combat takes THAC0 from each attacks[] entry, not the "
                               "sheet's thac0 — pass attacks= with the new per-weapon THAC0 too."]

        scalar = [f"{_STAT_LABEL.get(f, f)} {old} → {new}" for f, (old, new) in changes.items()
                  if not isinstance(new, (list, dict))]
        listed = [f for f, (_, new) in changes.items() if isinstance(new, (list, dict))]
        summary = ", ".join(scalar + [f"{f} updated" for f in listed])
        text = f"{label}: {summary}" + (f" ({reason})" if reason else "")
        _c.append_event(cfg, {"type": "stats_update", "character": pc_key,
                              "kind": kind, "changes": changes,
                              "reason": reason, "text": text})
        out["text"] = text
        return out

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
        To change a stat, use update_stats — it rewrites the sheet itself.
        This is for a sheet that has drifted from campaign.json."""
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
            "band":       _c.disposition_band(npc["disposition"])["label"],
            "faction":    npc.get("faction", ""),
            "modifier_applied_to_reaction": npc["disposition"] // 20,
        }

    @mcp.tool()
    def get_character(slug: str, brief: bool = False) -> dict:
        """Read a character file by slug.

        Returns the markdown content with a canonical mechanics header
        prepended from campaign.json (ability scores, level, HP, AC, THAC0,
        saves) so the read-path is never silent on a stat the prose omits —
        this closes the gap that led to archetype-guessing CON at level-up.
        Also returns the structured `mechanics` dict for programmatic use.

        brief: return the mechanics header plus only the opening section of the
        file instead of the whole sheet. A full PC sheet runs to ~8k characters
        (~1.8k tokens) and mid-scene the DM usually wants the numbers, not the
        backstory — the full read is still there when the prose matters."""
        cfg = _c.load_campaign()
        path = cfg["_data_dir"] / "characters" / f"{slug}.md"
        if not path.exists():
            return {"error": f"Character '{slug}' not found at {path}"}

        content = path.read_text(encoding="utf-8")
        char = _canonical_mechanics(cfg, slug)
        block = _mechanics_block(char) if char else ""

        if brief:
            body = content.strip()
            # Cut at the second heading: the first is the name, so this keeps
            # the opening description and drops the long tail of sections.
            headings = list(re.finditer(r'^##+\s', body, re.MULTILINE))
            if len(headings) > 1:
                body = body[:headings[1].start()].rstrip()
            if len(body) > _BRIEF_CHARS:
                body = body[:_BRIEF_CHARS].rstrip() + "\n…[truncated — call get_character without brief for the full sheet]"
            return {
                "slug":      slug,
                "brief":     True,
                "content":   f"{block}\n\n{body}" if block else body,
                "mechanics": char or {},
            }

        if block:
            content = f"{block}\n\n{content}"
        return {"slug": slug, "content": content, "mechanics": char or {}}

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
    def set_character_location(slug: str, location: str = "", chapter: str = "") -> dict:
        """Tag a character with a location/area and a chapter (or session)
        label, used for filtering on the dashboard /characters page.
        slug: character slug (the characters/<slug>.md filename without .md)
        location: where the character is found (a place name or area slug)
        chapter: a chapter/session label (e.g. 'Session 4', 'The Sunless Sea')
        Only non-empty fields are written; an omitted/empty field is left
        unchanged. introduce_npc auto-fills both from the party's current
        location and session — call this to correct or refine them."""
        cfg = _c.load_campaign()
        entry = _c.set_character_meta(
            cfg, slug,
            location=location if location else None,
            chapter=chapter if chapter else None,
        )
        return {"slug": slug, "meta": entry}

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
        content: initial markdown body
        Creating a file does not move the party — use move_party."""
        cfg = _c.load_campaign()
        locs_dir = cfg["_data_dir"] / "locations"
        locs_dir.mkdir(parents=True, exist_ok=True)

        area_file = locs_dir / f"{slug}.md"
        if not content.lstrip().startswith("#"):
            content = f"# {name}\n\n{content}"
        area_file.write_text(content, encoding="utf-8")

        # Create the subdirectory for places within this area
        (locs_dir / slug).mkdir(parents=True, exist_ok=True)

        return {"slug": slug, "path": str(area_file)}

    @mcp.tool()
    def create_location(slug: str, name: str, content: str, area: str = "") -> dict:
        """Create a location file (a place within an area, or a standalone location).
        slug: filesystem slug (e.g. 'the-sorcerers-lair')
        name: display name
        content: initial markdown body
        area: parent area slug, or empty for a top-level location
        Creating a file does not move the party — use move_party."""
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
