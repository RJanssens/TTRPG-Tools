"""Campaign state management — imported by all tool modules."""
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

BASE_DIR      = Path(__file__).parent
CAMPAIGNS_DIR = BASE_DIR / "campaigns"
ACTIVE_FILE   = BASE_DIR / ".active"

_cache: dict | None = None
_cache_mtime: float = 0.0
_state_cache: dict[str, tuple[float, dict]] = {}  # name -> (mtime, state)


def _invalidate():
    global _cache, _cache_mtime
    _cache = None
    _cache_mtime = 0.0
    _state_cache.clear()


def _mtime(p: Path) -> float:
    try:
        return p.stat().st_mtime
    except OSError:
        return 0.0


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write text to path via tmp+rename so a crash mid-write can't truncate.

    Why: state.json / events.json / campaign.json must never be partially
    written — a Ctrl-C in the wrong second would corrupt the campaign.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_active_name() -> str:
    if ACTIVE_FILE.exists():
        name = ACTIVE_FILE.read_text().strip()
        if name:
            return name
    dirs = sorted([
        d for d in CAMPAIGNS_DIR.iterdir()
        if d.is_dir() and (d / "campaign.json").exists()
    ])
    if dirs:
        return dirs[0].name
    raise RuntimeError("No active campaign. Use create_campaign first.")


def set_active(name: str):
    _invalidate()
    ACTIVE_FILE.write_text(name)


def campaign_dir(name: str | None = None) -> Path:
    return CAMPAIGNS_DIR / (name or get_active_name())


def load_campaign(name: str | None = None) -> dict:
    """Load campaign.json. mtime-gated: re-reads only if file changed since
    last load. Pass `name` to force a fresh read (skips cache)."""
    global _cache, _cache_mtime
    n = name or get_active_name()
    p = campaign_dir(n) / "campaign.json"
    if not p.exists():
        raise FileNotFoundError(f"Campaign '{n}' not found.")
    m = _mtime(p)
    if not name and _cache and _cache.get("_name") == n and m == _cache_mtime:
        return _cache
    cfg = json.loads(p.read_text(encoding="utf-8"))
    cfg["_name"] = n
    cfg["_dir"]  = campaign_dir(n)
    cfg["_data_dir"] = (campaign_dir(n) / cfg.get("data_dir", ".")).resolve()
    if not name:
        _cache = cfg
        _cache_mtime = m
    return cfg


def save_campaign(cfg: dict):
    """Write campaign.json, stripping private _ keys."""
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    atomic_write_text(cfg["_dir"] / "campaign.json", json.dumps(clean, indent=2))


def load_state(cfg: dict) -> dict:
    """Load state.json with mtime-gated cache. Returns the cached dict directly
    (callers mutate it in place; save_state writes back and updates the cache).

    Defense in depth: if the file is mid-write or hand-edited, JSONDecodeError
    short-circuits to the last cached state rather than crashing the dashboard."""
    name = cfg.get("_name", "")
    f = cfg["_dir"] / "state.json"
    if not f.exists():
        return _seed_state(cfg)
    m = _mtime(f)
    cached = _state_cache.get(name)
    if cached and cached[0] == m:
        return cached[1]
    try:
        state = json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        if cached:
            return cached[1]
        raise
    _state_cache[name] = (m, state)
    return state


def save_state(cfg: dict, state: dict):
    path = cfg["_dir"] / "state.json"
    atomic_write_text(path, json.dumps(state, indent=2))
    _state_cache[cfg.get("_name", "")] = (_mtime(path), state)


def _seed_state(cfg: dict) -> dict:
    state: dict = {
        "current_day":     1,
        "current_hour":    6,    # dawn
        "current_minute":  0,
        "current_session": 1,
        "coin": dict(cfg.get("initial_coin", {"gp": 0, "ep": 0, "sp": 0, "cp": 0})),
        "characters": {},
        "combat": None,
    }
    for key, char in cfg.get("characters", {}).items():
        raw_slots = char.get("spell_slots") or {}
        state["characters"][key] = {
            "hp":               char["hp_max"],
            "xp":               0,
            "conditions":       [],
            "spell_slots":      dict(raw_slots) if isinstance(raw_slots, dict) else {},
            "memorized_spells": list(char.get("memorized_spells", [])),
        }
    save_state(cfg, state)
    return state


def load_events(cfg: dict) -> list:
    """Read all events. Prefers events.jsonl (append-only) but falls back to
    legacy events.json (single JSON array) for older campaigns. If both exist,
    legacy entries are returned first, then JSONL appends."""
    out: list = []
    legacy = cfg["_dir"] / "events.json"
    if legacy.exists():
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            if isinstance(data, list):
                out.extend(data)
        except json.JSONDecodeError:
            pass
    jsonl = cfg["_dir"] / "events.jsonl"
    if jsonl.exists():
        with jsonl.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return out


def append_event(cfg: dict, event: dict):
    """Append one event as a single JSONL line — O(1) per append, no rewrite."""
    state = load_state(cfg)
    event.setdefault("day",       state.get("current_day", 1))
    event.setdefault("session",   state.get("current_session", 1))
    event.setdefault("timestamp", datetime.now().isoformat(timespec="seconds"))
    jsonl = cfg["_dir"] / "events.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    with jsonl.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def char_key_for(cfg: dict, label: str) -> tuple[str | None, dict | None]:
    """Return (key, char_dict) by label, case-insensitive prefix match."""
    low   = label.lower()
    chars = cfg.get("characters", {})
    for k, c in chars.items():
        if c["label"].lower() == low:
            return k, c
    for k, c in chars.items():
        if c["label"].lower().startswith(low):
            return k, c
    return None, None


def default_char_key(cfg: dict) -> str:
    return cfg.get("default_character") or next(iter(cfg.get("characters", {})), "")


# ---------------------------------------------------------------------------
# Character sheet writer — shared by campaign_mgmt and world tools
# ---------------------------------------------------------------------------

def write_character_sheet(cfg: dict, key: str, char: dict):
    """Write campaigns/<slug>/characters/<key>.md from a char dict.

    Renders all populated sections: background, ability scores, combat, attacks,
    saving throws, proficiencies, natural abilities, thief skills, spells, inventory.
    Sections with no data are silently omitted.
    """
    chars_dir = cfg["_data_dir"] / "characters"
    chars_dir.mkdir(exist_ok=True)

    label  = char.get("label", key)
    cls    = char.get("cls", "")
    race   = char.get("race", "human").title()
    gender = char.get("gender", "").title()
    level  = char.get("level", 0)
    hp_max = char.get("hp_max", 0)
    ac     = char.get("ac", 0)

    L: list[str] = []

    # ── Header ───────────────────────────────────────────────────────────────
    L += [f"# {label}", ""]
    parts = [p for p in [cls, race, gender, f"Level {level}" if level else ""] if p]
    L += ["  ".join(f"**{p}**" if i == 0 else p for i, p in enumerate(parts)), ""]

    # ── Background ───────────────────────────────────────────────────────────
    bg = char.get("background", "").strip()
    if bg:
        L += ["## Background", "", bg, ""]

    # ── Ability Scores ───────────────────────────────────────────────────────
    ab = char.get("ability_scores")
    if ab:
        keys  = ["str", "dex", "con", "int", "wis", "cha"]
        names = ["STR", "DEX", "CON", "INT", "WIS", "CHA"]
        vals  = [str(ab.get(k, "—")) for k in keys]
        L += [
            "## Ability Scores", "",
            "| " + " | ".join(names) + " |",
            "| " + " | ".join("---" for _ in names) + " |",
            "| " + " | ".join(vals) + " |",
            "",
        ]

    # ── Combat ───────────────────────────────────────────────────────────────
    combat_parts = []
    if hp_max:
        combat_parts.append(f"**HP:** {hp_max}")
    if ac:
        combat_parts.append(f"**AC:** {ac}")
    if combat_parts:
        L += ["## Combat", "", "  ".join(combat_parts), ""]

    attacks = char.get("attacks")
    if attacks:
        L += [
            "| Weapon | Speed | Attacks | THAC0 | Dmg (S/M) | Dmg (L) | Range |",
            "|--------|-------|---------|-------|-----------|---------|-------|",
        ]
        for atk in attacks:
            name    = atk.get("name", "—")
            speed   = atk.get("speed", "—")
            num_atk = atk.get("attacks", "1")
            t0      = atk.get("thac0", char.get("thac0", "—"))
            dsm     = atk.get("damage_sm", atk.get("damage", "—"))
            dl      = atk.get("damage_l", "—")
            rng     = atk.get("range", "—")
            L.append(f"| {name} | {speed} | {num_atk} | {t0} | {dsm} | {dl} | {rng} |")
        L.append("")
    elif char.get("thac0") or char.get("weapon"):
        bhit   = char.get("bonus_hit", 0)
        bdmg   = char.get("bonus_dmg", 0)
        weapon = char.get("weapon", "—")
        wspeed = char.get("weapon_speed", "—")
        thac0  = char.get("thac0", "—")
        hit_s  = f"+{bhit}" if isinstance(bhit, int) and bhit >= 0 else str(bhit)
        dmg_s  = f"+{bdmg}" if isinstance(bdmg, int) and bdmg >= 0 else str(bdmg)
        L += [
            "| Weapon | Speed | Attacks | THAC0 | Damage |",
            "|--------|-------|---------|-------|--------|",
            f"| {weapon} | {wspeed} | 1 | {thac0} | {weapon} ({hit_s}/{dmg_s}) |",
            "",
        ]

    # ── Saving Throws ────────────────────────────────────────────────────────
    saves = char.get("saves", [])
    if saves:
        save_names = ["Paralysis/Poison", "RSW", "Petrify/Poly", "Breath", "Spell"]
        head = " | ".join(save_names[:len(saves)])
        sep  = " | ".join("---" for _ in saves)
        vals = " | ".join(str(s) for s in saves)
        L += [
            "## Saving Throws", "",
            f"| {head} |",
            f"| {sep} |",
            f"| {vals} |",
            "",
        ]

    # ── Proficiencies ────────────────────────────────────────────────────────
    wp   = char.get("weapon_profs", [])
    nwps = char.get("nwps", [])
    if wp or nwps:
        L += ["## Proficiencies", ""]
        if wp:
            L += [f"**Weapon:** {', '.join(wp)}", ""]
        if nwps:
            def _nwp_str(n):
                if isinstance(n, dict):
                    ab_part = f" ({n['ability']})" if n.get("ability") else ""
                    slots   = f" ×{n['slots']}" if n.get("slots", 1) > 1 else ""
                    return n.get("name", "?") + ab_part + slots
                return str(n)
            L += [f"**Non-weapon:** {', '.join(_nwp_str(n) for n in nwps)}", ""]

    # ── Natural Abilities ────────────────────────────────────────────────────
    nat = char.get("natural_abilities", [])
    if nat:
        L += ["## Natural Abilities", ""]
        for ability in nat:
            L.append(f"- {ability}")
        L.append("")

    # ── Thief / Bard Skills ──────────────────────────────────────────────────
    skills = char.get("skills", {})
    if skills:
        L += ["## Thief Skills", "", "| Skill | % |", "|-------|---|"]
        for skill_name, pct in skills.items():
            L.append(f"| {skill_name.replace('_', ' ').title()} | {pct}% |")
        L.append("")

    # ── Spells ───────────────────────────────────────────────────────────────
    spell_slots = char.get("spell_slots")
    if spell_slots:
        memorized = char.get("memorized_spells", [])
        slot_str  = "  ".join(f"L{lvl}: {cnt}" for lvl, cnt in spell_slots.items())
        mem_str   = ", ".join(memorized) if memorized else "—"
        L += [
            "## Spells", "",
            f"**Slots:** {slot_str}", "",
            f"**Memorized:** {mem_str}", "",
        ]

    # ── Inventory ────────────────────────────────────────────────────────────
    inventory = char.get("inventory", [])
    if inventory:
        L += ["## Inventory", ""]
        for item in inventory:
            L.append(f"- {item}")
        L.append("")

    # ── Portrait prompt ──────────────────────────────────────────────────────
    pp = char.get("portrait_prompt", "").strip()
    if pp:
        L += ["## Portrait Prompt", "", f"> {pp}", ""]

    L.append("")
    (chars_dir / f"{key}.md").write_text("\n".join(L), encoding="utf-8")
