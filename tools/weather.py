"""Weather and seasons. Deterministic-feeling rolls with seasonal weighting.

Weather lives on state.json:
    state.current_season   — 'spring' | 'summer' | 'autumn' | 'winter'
    state.current_weather  — last rolled weather descriptor

Season can be set explicitly via set_season; otherwise it stays whatever was
last set (default 'spring' on a fresh state).
"""
import random
import _campaign as _c


SEASONS = ("spring", "summer", "autumn", "winter")

# (weight, label, effect_summary)
_TABLES: dict[str, dict[str, list[tuple[int, str, str]]]] = {
    "spring": {
        "wilderness": [
            (5, "clear",       "Light wind. No effect."),
            (4, "overcast",    "Heavy clouds. Visibility 1 mile."),
            (5, "light rain",  "Wet ground. Tracking +1 difficulty. Bows -1 to hit."),
            (3, "heavy rain",  "Soaked. Travel pace -25%. Missile -2 to hit. Fire spells partial fizzle."),
            (1, "thunderstorm", "Travel impossible. Tents needed. Lightning rare hazard. Encounter rolls -1."),
            (2, "morning fog", "Visibility 30 ft until midday. Surprise checks +1 to surprise either side."),
        ],
        "road":   None,   # uses wilderness
        "urban":  None,
    },
    "summer": {
        "wilderness": [
            (8, "clear",       "Bright and hot."),
            (3, "overcast",    "Sweltering haze. CON check at noon for armoured travellers."),
            (4, "heat wave",   "Travel pace -25% in heavy armour. Double water consumption."),
            (3, "afternoon storm", "Brief downpour at 14:00–16:00. Missile -2 to hit during. Lightning rare hazard."),
            (1, "drought wind", "Dry, dusty. Visibility 1/2 normal at distance."),
        ],
        "road":   None,
        "urban":  None,
    },
    "autumn": {
        "wilderness": [
            (5, "clear",       "Crisp. No effect."),
            (4, "overcast",    "Grey and cool."),
            (4, "rain",        "Wet roads. Travel pace -10%. Missile -1."),
            (3, "fog",         "Dense morning fog. Visibility 30 ft until late morning."),
            (2, "early frost", "Cold mornings. Light bedrolls insufficient — CON check or fatigue."),
            (1, "windstorm",   "Trees down across roads. Travel pace -50%. Missile -3."),
        ],
        "road":   None,
        "urban":  None,
    },
    "winter": {
        "wilderness": [
            (3, "clear cold",  "Bitter cold. CON check at night without proper shelter."),
            (3, "overcast cold","Grey and freezing. Wagon wheels stick on packed snow."),
            (4, "light snow",  "Tracking impossible after 1 hr. Travel pace -10%."),
            (3, "heavy snow",  "Travel pace -50%. Missile -3 to hit. Visibility 30 ft."),
            (2, "blizzard",    "Travel impossible. Shelter required. Frostbite risk per hour exposed."),
            (2, "freezing fog","Visibility 60 ft. Surface ice — DEX check or fall."),
        ],
        "road":   None,
        "urban":  None,
    },
}

# Dungeon weather is more about subterranean atmosphere
_DUNGEON_TABLE = [
    (10, "stale air",    "No effect."),
    (3,  "draft",         "Cold wind from somewhere — torches gutter, -10 min."),
    (2,  "humid air",     "Damp. Bowstrings slack — bows -1 to hit until restrung."),
    (1,  "miasma",        "Foul air. CON check or take 1 hp/turn until clear of source."),
]


def _terrain_table(season: str, terrain: str) -> list[tuple[int, str, str]]:
    season_key = season.lower() if season.lower() in SEASONS else "spring"
    if terrain.lower() == "dungeon":
        return _DUNGEON_TABLE
    season_tables = _TABLES.get(season_key, _TABLES["spring"])
    table = season_tables.get(terrain.lower())
    if table is None:
        table = season_tables.get("wilderness")
    return table


def _weighted_pick(table: list[tuple[int, str, str]]) -> tuple[str, str]:
    total = sum(w for w, _, _ in table)
    r = random.randint(1, total)
    acc = 0
    for w, label, effect in table:
        acc += w
        if r <= acc:
            return label, effect
    return table[-1][1], table[-1][2]


def register(mcp):

    @mcp.tool()
    def set_season(season: str) -> dict:
        """Set the current in-game season. One of: spring, summer, autumn, winter.
        Affects the weather_check tables. Default is 'spring' on a fresh campaign."""
        s = season.lower().strip()
        if s not in SEASONS:
            return {"error": f"Unknown season '{season}'. Use: {', '.join(SEASONS)}."}
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        state["current_season"] = s
        _c.save_state(cfg, state)
        return {"season": s}

    @mcp.tool()
    def weather_check(terrain: str = "wilderness") -> dict:
        """Roll weather for the current season and the given terrain.
        terrain: wilderness | road | urban | dungeon (defaults to wilderness)
        Updates state.current_weather and returns the descriptor + mechanical effects.

        Call at session start, at dawn each travel day, and on any major terrain change.
        Don't roll silently — apply the effect to subsequent missile attacks, travel pace,
        and encounter checks."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        season = state.get("current_season", "spring")
        table = _terrain_table(season, terrain)
        label, effect = _weighted_pick(table)

        descriptor = f"{label} ({terrain}, {season})"
        state["current_weather"] = descriptor
        _c.save_state(cfg, state)

        return {
            "season":  season,
            "terrain": terrain,
            "weather": label,
            "effect":  effect,
            "descriptor": descriptor,
        }

    @mcp.tool()
    def current_weather() -> dict:
        """Return the current weather (and season). Read-only — does not roll."""
        cfg = _c.load_campaign()
        state = _c.load_state(cfg)
        return {
            "season":  state.get("current_season", "spring"),
            "weather": state.get("current_weather", ""),
        }
