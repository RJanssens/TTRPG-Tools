#!/usr/bin/env python3
"""Campaign dashboard web app. Run: python3 dashboard.py [--campaign NAME] [--port 5000]"""

import argparse
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_from_directory

import _campaign as _c

_MONSTERS_DB  = Path(__file__).parent / "global" / "monsters.db"
_MONSTERS_DIR = Path(__file__).parent / "global" / "monsters"
_2E_DB        = Path(__file__).parent / "global" / "2e.db"

# Cached read-only connections. The reference DBs (monsters/2e/rules) are
# read-only at runtime, so a single connection per DB shared across Flask
# threads is safe with check_same_thread=False. Avoids the per-request
# connect cost on hot endpoints like /combat-state.
_db_connections: dict[str, sqlite3.Connection] = {}


def _db(path: Path) -> sqlite3.Connection:
    """Get a cached read-only connection to a reference SQLite DB."""
    key = str(path)
    conn = _db_connections.get(key)
    if conn is None:
        conn = sqlite3.connect(key, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        _db_connections[key] = conn
    return conn

# ---------------------------------------------------------------------------
# Global state — set in main()
# ---------------------------------------------------------------------------
cfg: dict = {}
app = Flask(__name__)


_cfg_mtime: float = 0.0


def _reload_cfg() -> None:
    """Re-read campaign.json so quests/characters/factions added mid-session
    via MCP tools show up without restarting the dashboard. mtime-gated:
    only re-reads when campaign.json has changed since the last refresh."""
    global _cfg_mtime
    if not cfg:
        return
    p = cfg["_dir"] / "campaign.json"
    try:
        m = p.stat().st_mtime
    except OSError:
        return
    if m == _cfg_mtime:
        return
    # Pass the name to bypass load_campaign's module cache and return a fresh
    # dict — the cached dict is shared with the MCP tools and clearing it
    # there would empty `cfg` too.
    fresh = _c.load_campaign(cfg["_name"])
    cfg.clear()
    cfg.update(fresh)
    _cfg_mtime = m


@app.before_request
def _refresh_before_request() -> None:
    _reload_cfg()

# ---------------------------------------------------------------------------
# Shared HTML base template
# ---------------------------------------------------------------------------
BASE_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    background: #1a1510;
    color: #d4c5a9;
    font-family: Georgia, 'Times New Roman', serif;
    font-size: 16px;
    line-height: 1.6;
  }}
  .container {{
    max-width: 900px;
    margin: 0 auto;
    padding: 20px;
  }}
  nav {{
    background: #110e0a;
    border-bottom: 1px solid #4a3828;
    padding: 10px 20px;
    margin-bottom: 24px;
  }}
  nav a {{
    color: #c8a96e;
    text-decoration: none;
    margin-right: 18px;
    font-size: 0.95em;
    letter-spacing: 0.03em;
  }}
  nav a:hover {{ color: #e8c98e; text-decoration: underline; }}
  h1, h2, h3, h4 {{ color: #c8a96e; margin: 16px 0 8px; }}
  h1 {{ font-size: 1.8em; border-bottom: 1px solid #4a3828; padding-bottom: 8px; }}
  h2 {{ font-size: 1.3em; }}
  h3 {{ font-size: 1.1em; }}
  p {{ margin: 8px 0; }}
  ul, ol {{ margin: 8px 0 8px 24px; }}
  a {{ color: #c8a96e; }}
  a:hover {{ color: #e8c98e; }}
  .card {{
    background: #2a2018;
    border: 1px solid #4a3828;
    border-radius: 6px;
    padding: 16px;
    margin: 8px 0;
  }}
  .card h2 {{ margin-top: 0; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 16px;
    margin-top: 16px;
  }}
  .grid-item {{
    background: #2a2018;
    border: 1px solid #4a3828;
    border-radius: 6px;
    overflow: hidden;
  }}
  .grid-item img {{
    width: 100%;
    display: block;
    border-radius: 4px 4px 0 0;
  }}
  .grid-item .caption {{
    padding: 10px 12px;
    font-size: 0.9em;
    color: #b0a080;
  }}
  .grid-item .caption strong {{
    display: block;
    color: #d4c5a9;
    margin-bottom: 4px;
  }}
  .hp-bar {{
    display: inline-block;
    font-family: monospace;
    letter-spacing: 1px;
    font-size: 0.9em;
  }}
  .hp-bar .filled {{ color: #6db86d; }}
  .hp-bar .empty  {{ color: #5a3a3a; }}
  .info-bar {{
    background: #221a12;
    border: 1px solid #4a3828;
    border-radius: 6px;
    padding: 10px 14px;
    margin: 0 0 16px 0;
    display: flex;
    flex-wrap: wrap;
    gap: 14px 24px;
    font-size: 0.95em;
  }}
  .info-bar .item strong {{ color: #c8a96e; margin-right: 4px; }}
  .clock {{
    display: inline-block;
    padding: 4px 10px;
    border-radius: 3px;
    border: 1px solid #4a3828;
    background: #2a2018;
    font-size: 0.9em;
    margin: 2px 4px 2px 0;
  }}
  .clock.urgent {{ border-color: #b56a3a; color: #e8b487; background: #3a2418; }}
  .clock.expired {{ border-color: #6a2818; color: #ff7a5a; background: #3a1812; }}
  .truth-true        {{ color: #6db86d; font-size: 0.8em; }}
  .truth-partly_true {{ color: #c8a96e; font-size: 0.8em; }}
  .truth-false       {{ color: #c87a7a; font-size: 0.8em; }}
  .truth-fabrication {{ color: #ff5a4a; font-size: 0.8em; font-weight: bold; }}
  .secret-card {{
    background: #2a1818;
    border: 1px solid #6a3828;
    border-left: 4px solid #c8503a;
    padding: 12px 16px;
    margin: 8px 0;
    border-radius: 4px;
  }}
  .scope-warning {{ color: #e8b487; font-size: 0.85em; }}
  .tag {{
    display: inline-block;
    background: #3a2818;
    border: 1px solid #5a4030;
    border-radius: 3px;
    padding: 1px 7px;
    font-size: 0.8em;
    margin: 2px;
    color: #c8a96e;
  }}
  .two-col {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
  }}
  @media (max-width: 600px) {{ .two-col {{ grid-template-columns: 1fr; }} }}
  .portrait {{
    float: right;
    margin: 0 0 12px 16px;
    max-width: 180px;
    border-radius: 4px;
    border: 1px solid #4a3828;
  }}
  .clearfix::after {{ content: ''; display: table; clear: both; }}
  .muted {{ color: #8a7a60; font-size: 0.9em; }}
  pre {{ background: #110e0a; padding: 12px; border-radius: 4px; overflow-x: auto; font-size: 0.85em; }}
  code {{ font-family: monospace; }}
  blockquote {{ border-left: 3px solid #4a3828; padding-left: 12px; color: #a09070; }}
  hr {{ border: none; border-top: 1px solid #4a3828; margin: 20px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; }}
  th, td {{ border: 1px solid #4a3828; padding: 6px 10px; text-align: left; }}
  th {{ background: #2a2018; color: #c8a96e; }}
  img:not(#lb-img) {{ cursor: zoom-in; }}
  #lb {{
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.88); z-index: 9999;
    align-items: center; justify-content: center;
    padding: 20px; cursor: zoom-out;
  }}
  #lb.open {{ display: flex; }}
  #lb img {{
    max-width: 90vw; max-height: 90vh; object-fit: contain;
    border-radius: 6px; border: 2px solid #5a4030;
    box-shadow: 0 8px 40px rgba(0,0,0,.9); cursor: zoom-out;
  }}
</style>
</head>
<body>
<nav>
  <a href="/">Gallery</a>
  <a href="/party">Party</a>
  <a href="/world">World</a>
  <a href="/quests">Quests</a>
  <a href="/log">Chronicle</a>
  <a href="/characters">Characters</a>
  <a href="/locations">Locations</a>
  <a href="/monsters">Monsters</a>
  <a href="/spells">Spells</a>
  <a href="/classes">Classes</a>
  <a href="/items">Items</a>
  <a href="/combat">Combat</a>
</nav>
<div class="container">
{body}
</div>
<div id="lb"><img id="lb-img" src="" alt=""></div>
<script>
(function(){{
  var lb=document.getElementById('lb');
  var li=document.getElementById('lb-img');
  document.addEventListener('click',function(e){{
    var t=e.target;
    if(lb.classList.contains('open')){{lb.classList.remove('open');return;}}
    if(t.tagName==='IMG'&&t.id!=='lb-img'){{li.src=t.src;li.alt=t.alt;lb.classList.add('open');}}
  }});
  document.addEventListener('keydown',function(e){{if(e.key==='Escape')lb.classList.remove('open');}});
}})();
</script>
</body>
</html>"""


def render(title: str, body: str) -> str:
    return BASE_TEMPLATE.format(title=html.escape(title), body=body)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_image_index() -> list:
    idx = cfg["_data_dir"] / "images" / "index.json"
    if not idx.exists():
        return []
    try:
        return json.loads(idx.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _find_portrait(name: str, index: list, slug: str = "") -> dict | None:
    """Return the most recent portrait matching by slug (preferred) or name substring."""
    slug_lower = slug.lower()
    name_lower = name.lower()
    # Iterate reversed so the most recently added entry wins
    for entry in reversed(index):
        if entry.get("type") != "portrait":
            continue
        if slug_lower and entry.get("slug", "").lower() == slug_lower:
            return entry
    for entry in reversed(index):
        if entry.get("type") != "portrait":
            continue
        if name_lower in entry.get("scene", "").lower():
            return entry
    return None


def _inline(s: str) -> str:
    """Render inline Markdown (bold, italic, code, links) in an already-escaped string."""
    s = re.sub(r'\*\*\*(.+?)\*\*\*', r'<strong><em>\1</em></strong>', s)
    s = re.sub(r'\*\*(.+?)\*\*',     r'<strong>\1</strong>', s)
    s = re.sub(r'\*(.+?)\*',          r'<em>\1</em>', s)
    s = re.sub(r'_(.+?)_',            r'<em>\1</em>', s)
    s = re.sub(r'`(.+?)`',            r'<code>\1</code>', s)
    s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
    return s


def _markdown_to_html(md: str) -> str:
    """Minimal Markdown → HTML converter (headings, bold, italic, lists, tables, hr, paragraphs)."""
    lines = md.split("\n")
    out = []
    in_ul = False
    in_ol = False
    in_pre = False
    in_table = False
    table_header_done = False
    buf: list[str] = []

    def flush_para():
        nonlocal buf
        if buf:
            text = " ".join(buf).strip()
            if text:
                out.append(f"<p>{_inline(text)}</p>")
            buf = []

    def _table_cells(line: str, tag: str) -> str:
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        return "<tr>" + "".join(f"<{tag}>{_inline(html.escape(c))}</{tag}>" for c in cells) + "</tr>"

    def close_lists():
        nonlocal in_ul, in_ol
        if in_ul:
            out.append("</ul>")
            in_ul = False
        if in_ol:
            out.append("</ol>")
            in_ol = False

    def close_table():
        nonlocal in_table, table_header_done
        if in_table:
            out.append("</tbody></table>")
            in_table = False
            table_header_done = False

    for line in lines:
        # Fenced code blocks
        if line.startswith("```"):
            if in_pre:
                out.append("</code></pre>")
                in_pre = False
            else:
                flush_para()
                close_lists()
                close_table()
                out.append("<pre><code>")
                in_pre = True
            continue
        if in_pre:
            out.append(html.escape(line))
            continue

        # Table rows: lines starting and ending with | (or containing |)
        is_table_row = line.strip().startswith("|") and "|" in line.strip()[1:]
        is_separator = is_table_row and re.match(r'^[\|\s\-:]+$', line)

        if is_table_row:
            flush_para()
            close_lists()
            if is_separator:
                # Separator row — switch from thead to tbody
                if in_table and not table_header_done:
                    out.append("</thead><tbody>")
                    table_header_done = True
            elif not in_table:
                out.append('<table><thead>')
                in_table = True
                table_header_done = False
                out.append(_table_cells(line, "th"))
            elif not table_header_done:
                out.append(_table_cells(line, "th"))
            else:
                out.append(_table_cells(line, "td"))
            continue
        else:
            close_table()

        # Headings
        m = re.match(r'^(#{1,6})\s+(.*)', line)
        if m:
            flush_para()
            close_lists()
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(html.escape(m.group(2)))}</h{lvl}>")
            continue

        # HR
        if re.match(r'^(-{3,}|_{3,}|\*{3,})$', line.strip()):
            flush_para()
            close_lists()
            out.append("<hr>")
            continue

        # Blockquote
        if line.startswith("> "):
            flush_para()
            close_lists()
            out.append(f"<blockquote>{_inline(html.escape(line[2:]))}</blockquote>")
            continue

        # Unordered list
        m = re.match(r'^[\*\-\+]\s+(.*)', line)
        if m:
            flush_para()
            if not in_ul:
                if in_ol:
                    out.append("</ol>")
                    in_ol = False
                out.append("<ul>")
                in_ul = True
            out.append(f"<li>{_inline(html.escape(m.group(1)))}</li>")
            continue

        # Ordered list
        m = re.match(r'^\d+\.\s+(.*)', line)
        if m:
            flush_para()
            if not in_ol:
                if in_ul:
                    out.append("</ul>")
                    in_ul = False
                out.append("<ol>")
                in_ol = True
            out.append(f"<li>{_inline(html.escape(m.group(1)))}</li>")
            continue

        close_lists()

        if not line.strip():
            flush_para()
        else:
            buf.append(html.escape(line))

    flush_para()
    close_lists()
    close_table()
    return "\n".join(out)


def _period_for_hour(h: int) -> str:
    if h < 5:    return "night"
    if h < 8:    return "dawn"
    if h < 12:   return "morning"
    if h < 14:   return "midday"
    if h < 18:   return "afternoon"
    if h < 21:   return "evening"
    return "night"


def _world_info_bar(state: dict) -> str:
    """Render the day/time/weather/season/clocks summary used on multiple pages."""
    day = state.get("current_day", 1)
    session = state.get("current_session", 0)
    h = state.get("current_hour", 6)
    m = state.get("current_minute", 0)
    period = _period_for_hour(h)
    season = state.get("current_season", "spring")
    weather = state.get("current_weather", "—")
    weather_label = weather.split(" (", 1)[0] if weather else "—"

    coin = state.get("coin", {})
    coin_parts = []
    for denom in ("gp", "ep", "sp", "cp"):
        v = coin.get(denom, 0)
        if v:
            coin_parts.append(f"{v} {denom}")
    coin_str = ", ".join(coin_parts) if coin_parts else "0 gp"

    return (
        '<div class="info-bar">'
        f'<span class="item"><strong>Day:</strong>{day}</span>'
        f'<span class="item"><strong>Session:</strong>{session}</span>'
        f'<span class="item"><strong>Time:</strong>{h:02d}:{m:02d} <em class="muted">({period})</em></span>'
        f'<span class="item"><strong>Season:</strong>{html.escape(season)}</span>'
        f'<span class="item"><strong>Weather:</strong>{html.escape(weather_label)}</span>'
        f'<span class="item"><strong>Coin:</strong>{html.escape(coin_str)}</span>'
        '</div>'
    )


def _hp_bar(current: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return ""
    filled = round(width * max(current, 0) / maximum)
    empty = width - filled
    bar = f'<span class="hp-bar"><span class="filled">{"█" * filled}</span><span class="empty">{"░" * empty}</span></span>'
    return bar


def _read_first_heading(path: Path) -> str:
    if not path.exists():
        return path.stem
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#"):
            return line.lstrip("#").strip()
    return path.stem


def _read_first_paragraph(path: Path) -> str:
    if not path.exists():
        return ""
    in_heading = True
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            in_heading = True
            continue
        if line.strip() == "":
            if not in_heading:
                break
            continue
        in_heading = False
        return line.strip()
    return ""


# ---------------------------------------------------------------------------
# Monster portrait helpers
# ---------------------------------------------------------------------------

def _monster_portrait_path(slug: str) -> Path | None:
    """Return path to an existing monster portrait file, or None."""
    for ext in ("png", "jpg", "jpeg", "webp"):
        p = _MONSTERS_DIR / f"{slug}.{ext}"
        if p.exists():
            return p
    return None


def _monster_portraits_dict() -> dict:
    """Return {slug: filename} for every monster that has a portrait."""
    if not _MONSTERS_DIR.exists():
        return {}
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    return {p.stem: p.name for p in _MONSTERS_DIR.iterdir() if p.suffix.lower() in exts}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def gallery():
    index = _load_image_index()
    scenes = [e for e in index if e.get("type") == "scene"]
    campaign_name = cfg.get("name", "Campaign")

    if not scenes:
        body = f"<h1>{html.escape(campaign_name)} — Gallery</h1><p class='muted'>No scene images yet.</p>"
        return render(campaign_name, body)

    items = []
    for e in reversed(scenes):
        fn = html.escape(e.get("filename", ""))
        scene = html.escape(e.get("scene", ""))
        desc = html.escape(e.get("description", ""))
        items.append(
            f'<div class="grid-item">'
            f'<img src="/images/{fn}" alt="{scene}" loading="lazy">'
            f'<div class="caption"><strong>{scene}</strong>{desc}</div>'
            f'</div>'
        )

    body = (
        f"<h1>{html.escape(campaign_name)} — Gallery</h1>"
        f'<div class="grid">{"".join(items)}</div>'
    )
    return render(f"{campaign_name} — Gallery", body)


@app.route("/party")
def party():
    state = _c.load_state(cfg)
    campaign_name = cfg.get("name", "Campaign")
    characters = cfg.get("characters", {})

    coin = state.get("coin", {})
    coin_parts = []
    for denom in ("gp", "ep", "sp", "cp"):
        v = coin.get(denom, 0)
        if v:
            coin_parts.append(f"{v} {denom}")
    coin_str = ", ".join(coin_parts) if coin_parts else "0 gp"

    day = state.get("current_day", 1)
    session = state.get("current_session", 0)

    cards = []
    for key, char in characters.items():
        cstate = state.get("characters", {}).get(key, {})
        hp_max = char.get("hp_max", 0)
        hp_cur = cstate.get("hp", hp_max)
        label = char.get("label", key)
        cls = char.get("cls", "")
        xp = cstate.get("xp", 0)
        conditions = cstate.get("conditions", [])

        bar = _hp_bar(hp_cur, hp_max)
        cond_tags = "".join(f'<span class="tag">{html.escape(c)}</span>' for c in conditions)

        # Spell slots
        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}
        slot_lines = []
        for lvl, max_count in slot_cfg.items():
            remaining = slot_state.get(str(lvl), max_count)
            slot_lines.append(f"L{lvl}: {remaining}/{max_count}")
        slots_html = ""
        if slot_lines:
            slots_html = "<p><strong>Spells:</strong> " + " &nbsp; ".join(slot_lines) + "</p>"

        cards.append(
            f'<div class="card">'
            f'<h2><a href="/sheets/{html.escape(key)}">{html.escape(label)}</a></h2>'
            f"<p class='muted'>{html.escape(cls)}</p>"
            f"<p><strong>HP:</strong> {html.escape(str(hp_cur))}/{html.escape(str(hp_max))} {bar}</p>"
            f"<p><strong>XP:</strong> {xp:,}</p>"
            f"{slots_html}"
            f'<p>{cond_tags}</p>'
            f"</div>"
        )

    # Loot pile staged from award_treasure
    loot_pile = state.get("loot_pile", [])
    loot_html = ""
    if loot_pile:
        rows = []
        for i, item in enumerate(loot_pile):
            kind = html.escape(item.get("kind", "?"))
            desc = html.escape(item.get("description", "?"))
            val = item.get("value_gp", 0)
            cat = html.escape(item.get("category", ""))
            cat_str = f' <span class="tag">{cat}</span>' if cat else ""
            rows.append(
                f'<li>[{i}] <strong>{desc}</strong> '
                f'<span class="muted">— {kind}, {val} gp</span>{cat_str}</li>'
            )
        loot_html = (
            f'<div class="card"><h2>Loot Pile <span class="muted">'
            f'({len(loot_pile)} items)</span></h2>'
            f'<ul>{"".join(rows)}</ul>'
            f'<p class="muted" style="font-size:0.85em">'
            f'Distribute via <code>claim_loot(index, character)</code>.</p></div>'
        )

    # Tracked consumables per character
    consumables = state.get("consumables", {})
    consumables_rows = []
    for key, items in consumables.items():
        if not items:
            continue
        char = cfg.get("characters", {}).get(key) or cfg.get("npcs", {}).get(key, {})
        label = char.get("label", key)
        item_strs = ", ".join(f"{html.escape(name)}×{qty}" for name, qty in items.items())
        consumables_rows.append(f"<li><strong>{html.escape(label)}:</strong> {item_strs}</li>")
    consumables_html = ""
    if consumables_rows:
        consumables_html = (
            f'<div class="card"><h2>Consumables</h2>'
            f'<ul>{"".join(consumables_rows)}</ul></div>'
        )

    # Light sources
    lights = state.get("light_sources", [])
    lit = [s for s in lights if s.get("lit")]
    light_html = ""
    if lit:
        rows = []
        for s in lit:
            mins = s.get("minutes_remaining", 0)
            cls = "urgent" if mins <= 5 else ""
            holder = html.escape(s.get("holder", "?"))
            stype = html.escape(s.get("type", "?"))
            rows.append(f'<span class="clock {cls}">{stype} ({holder}, {mins} min)</span>')
        light_html = (
            f'<div class="card"><h2>Light Sources</h2>{"".join(rows)}</div>'
        )

    body = (
        f"<h1>{html.escape(campaign_name)} — Party</h1>"
        + _world_info_bar(state)
        + "".join(cards)
        + light_html
        + consumables_html
        + loot_html
    )
    return render(f"{campaign_name} — Party", body)


@app.route("/world")
def world_view():
    state = _c.load_state(cfg)
    campaign_name = cfg.get("name", "Campaign")

    # Factions
    factions = cfg.get("factions", {})
    faction_state = state.get("faction_state", {})
    faction_cards = []
    for slug, f in factions.items():
        fs = faction_state.get(slug, {})
        rep = fs.get("reputation", 0)
        strength = fs.get("strength", 100)
        rep_color = "#6db86d" if rep > 20 else ("#c87a7a" if rep < -20 else "#c8a96e")
        known_tag = '<span class="tag">known</span>' if f.get("known_to_party") else \
                    '<span class="tag" style="background:#3a1818;color:#c87a7a">hidden</span>'
        faction_cards.append(
            f'<div class="card">'
            f'<h2>{html.escape(f.get("name", slug))} '
            f'<span class="muted" style="font-size:0.7em">[{html.escape(f.get("alignment",""))}]</span></h2>'
            f'<p class="muted">{html.escape(f.get("scope","local"))} scope · slug: <code>{html.escape(slug)}</code> {known_tag}</p>'
            f'<p>{html.escape(f.get("goals",""))}</p>'
            f'<p><strong>Strength:</strong> {strength}% '
            f'&nbsp;·&nbsp; <strong>Reputation:</strong> '
            f'<span style="color:{rep_color}">{rep:+d}</span></p>'
            f'</div>'
        )
    factions_html = ("".join(faction_cards) if faction_cards
                     else '<p class="muted">No factions registered. Use add_faction to seed.</p>')

    # Clocks
    clocks = state.get("faction_clocks", [])
    clocks_active = sorted(
        [c for c in clocks if c.get("days_remaining", 0) > 0],
        key=lambda c: c["days_remaining"],
    )
    clocks_done = [c for c in clocks if c.get("days_remaining", 0) <= 0]

    clock_rows = []
    for c in clocks_active:
        days = c["days_remaining"]
        cls = "urgent" if days <= 7 else ""
        fac = c.get("faction", "")
        fac_html = f' · {html.escape(fac)}' if fac else ""
        on_complete = c.get("on_complete", "")
        oc_html = f'<br><span class="muted">{html.escape(on_complete)}</span>' if on_complete else ""
        clock_rows.append(
            f'<div class="clock {cls}" style="display:block;padding:10px 14px;margin:6px 0">'
            f'<strong>{html.escape(c.get("label","?"))}</strong> '
            f'<span style="float:right">{days} days</span>{fac_html}{oc_html}'
            f'</div>'
        )
    expired_rows = []
    for c in clocks_done:
        expired_rows.append(
            f'<div class="clock expired" style="display:block;padding:10px 14px;margin:6px 0">'
            f'<strong>{html.escape(c.get("label","?"))}</strong> '
            f'<span style="float:right">expired</span>'
            f'</div>'
        )
    clocks_html = ""
    if clock_rows:
        clocks_html += "".join(clock_rows)
    if expired_rows:
        clocks_html += "<h3>Expired (consequences pending)</h3>" + "".join(expired_rows)
    if not clocks_html:
        clocks_html = '<p class="muted">No active clocks.</p>'

    # Rumors (DM view — show truth tier)
    rumors = cfg.get("rumors", [])
    rumor_rows = []
    for r in rumors[-15:]:
        truth = r.get("truth", "partly_true")
        rumor_rows.append(
            f'<li><span class="truth-{html.escape(truth)}">[{html.escape(truth)}]</span> '
            f'{html.escape(r.get("text",""))}'
            f'</li>'
        )
    rumors_html = (
        f'<ul>{"".join(rumor_rows)}</ul>' if rumor_rows
        else '<p class="muted">No rumors stored.</p>'
    )

    # Secrets (DM-only)
    secrets_path = cfg["_dir"] / "secrets.json"
    secret_rows = []
    if secrets_path.exists():
        try:
            recs = json.loads(secrets_path.read_text(encoding="utf-8"))
            for r in recs[-15:]:
                tags = r.get("tags", [])
                tag_html = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in tags)
                rel = r.get("related_to", "")
                rel_html = f' <span class="muted">→ {html.escape(rel)}</span>' if rel else ""
                revealed = r.get("revealed")
                rev_html = '<span class="tag" style="background:#1a3a18;color:#6db86d">revealed</span>' if revealed else ""
                secret_rows.append(
                    f'<div class="secret-card">'
                    f'<p>{html.escape(r.get("text",""))}{rel_html}</p>'
                    f'<p class="muted" style="font-size:0.8em">'
                    f'#{r.get("id","?")} · day {r.get("day","?")} {tag_html} {rev_html}</p>'
                    f'</div>'
                )
        except (json.JSONDecodeError, OSError):
            pass
    secrets_html = (
        "".join(secret_rows) if secret_rows
        else '<p class="muted">No DM-only notes recorded.</p>'
    )

    body = (
        f"<h1>{html.escape(campaign_name)} — World</h1>"
        + _world_info_bar(state)
        + "<h2>Factions</h2>" + factions_html
        + "<h2>World Clocks</h2>" + clocks_html
        + "<h2>Rumors <span class='muted' style='font-size:0.6em'>(DM view — shows truth tier)</span></h2>"
        + rumors_html
        + "<h2>DM-Only Notes <span class='muted' style='font-size:0.6em'>(never narrate to player)</span></h2>"
        + secrets_html
    )
    return render(f"{campaign_name} — World", body)


@app.route("/quests")
def quests_view():
    state = _c.load_state(cfg)
    campaign_name = cfg.get("name", "Campaign")
    quests = cfg.get("quests", {})

    chars = cfg.get("characters", {})
    avg_level = (sum(int(c.get("level", 1)) for c in chars.values()) / len(chars)) if chars else 1.0

    def _scope_warn(scope: str) -> str:
        if scope == "regional" and avg_level < 4:
            return f"⚠ regional scope on L{avg_level:.1f} party"
        if scope == "continental" and avg_level < 8:
            return f"⚠ continental scope on L{avg_level:.1f} party"
        return ""

    by_status: dict[str, list] = {"active": [], "paused": [], "complete": [], "failed": []}
    for slug, q in quests.items():
        by_status.setdefault(q.get("status", "active"), []).append((slug, q))

    def _render_quest(slug: str, q: dict) -> str:
        scope = q.get("scope", "local")
        warn = _scope_warn(scope)
        warn_html = f'<p class="scope-warning">{html.escape(warn)}</p>' if warn else ""
        log = q.get("notes_log", [])
        log_html = ""
        if log:
            items = "".join(
                f"<li><span class='muted'>day {entry.get('day','?')}:</span> "
                f"{html.escape(entry.get('text',''))}</li>"
                for entry in log[-5:]
            )
            log_html = f"<details><summary class='muted'>Recent notes</summary><ul>{items}</ul></details>"
        related = []
        for kind in ("npcs", "factions", "locations"):
            v = q.get(f"related_{kind}", [])
            if v:
                related.append(f"<strong>{kind}:</strong> {', '.join(html.escape(s) for s in v)}")
        rel_html = f"<p class='muted'>{' &nbsp;·&nbsp; '.join(related)}</p>" if related else ""
        giver = q.get("giver", "")
        giver_html = f"<p class='muted'>Given by <code>{html.escape(giver)}</code></p>" if giver else ""
        return (
            f'<div class="card">'
            f'<h2>{html.escape(q.get("title", slug))} '
            f'<span class="tag">{html.escape(scope)}</span></h2>'
            f'{warn_html}{giver_html}'
            f'<p>{html.escape(q.get("stakes",""))}</p>'
            f'{rel_html}{log_html}'
            f'</div>'
        )

    sections = []
    for status_key in ("active", "paused", "complete", "failed"):
        items = by_status.get(status_key, [])
        if not items:
            continue
        sections.append(f"<h2>{status_key.title()}</h2>")
        for slug, q in sorted(items, key=lambda kv: kv[1].get("started_day", 0)):
            sections.append(_render_quest(slug, q))

    if not sections:
        sections.append("<p class='muted'>No quests recorded yet. Use add_quest to start.</p>")

    body = (
        f"<h1>{html.escape(campaign_name)} — Quests</h1>"
        f"<p class='muted'>Average party level: {avg_level:.1f}</p>"
        + "".join(sections)
    )
    return render(f"{campaign_name} — Quests", body)


@app.route("/log")
def log():
    campaign_name = cfg.get("name", "Campaign")
    log_file = cfg.get("session_log_file") or cfg.get("chronicle_file") or "adventure_log.md"
    log_path = cfg["_data_dir"] / log_file

    if not log_path.exists():
        body = f"<h1>{html.escape(campaign_name)} — Chronicle</h1><p class='muted'>No chronicle yet.</p>"
        return render(f"{campaign_name} — Chronicle", body)

    content = log_path.read_text(encoding="utf-8")
    content_html = _markdown_to_html(content)

    body = (
        f"<h1>{html.escape(campaign_name)} — Chronicle</h1>"
        f'<div class="card">{content_html}</div>'
    )
    return render(f"{campaign_name} — Chronicle", body)


@app.route("/sheets/<slug>")
def character_sheet(slug: str):
    campaign_name = cfg.get("name", "Campaign")
    chars_dir = cfg["_data_dir"] / "characters"
    path = chars_dir / f"{slug}.md"

    if not path.exists():
        abort(404)

    content = path.read_text(encoding="utf-8")
    content_html = _markdown_to_html(content)
    name = _read_first_heading(path)

    index = _load_image_index()
    portrait = _find_portrait(name, index, slug)
    portrait_html = ""
    if portrait:
        fn   = html.escape(portrait["filename"])
        desc = html.escape(portrait.get("description", ""))
        portrait_html = (
            f'<div style="float:right;margin:0 0 16px 20px;max-width:200px;text-align:center">'
            f'<img src="/images/{fn}" alt="{html.escape(name)}" style="width:100%;border-radius:4px;border:1px solid #4a3828">'
            f'{"<p class=muted style=font-size:.85em;margin-top:6px>" + desc + "</p>" if desc else ""}'
            f'</div>'
        )

    body = (
        f'<div class="card clearfix">'
        f"{portrait_html}"
        f"{content_html}"
        f"</div>"
    )
    return render(f"{html.escape(name)} — {html.escape(campaign_name)}", body)


@app.route("/characters")
def characters():
    campaign_name = cfg.get("name", "Campaign")
    chars_dir = cfg["_data_dir"] / "characters"
    index = _load_image_index()

    if not chars_dir.exists():
        body = f"<h1>{html.escape(campaign_name)} — Characters</h1><p class='muted'>No characters yet.</p>"
        return render(f"{campaign_name} — Characters", body)

    cards = []
    for md in sorted(chars_dir.glob("*.md")):
        name = _read_first_heading(md)
        blurb = _read_first_paragraph(md)
        slug = md.stem
        portrait = _find_portrait(name, index, slug)

        img_html = ""
        if portrait:
            fn = html.escape(portrait["filename"])
            img_html = f'<img src="/images/{fn}" alt="{html.escape(name)}" class="portrait">'

        cards.append(
            f'<div class="card clearfix">'
            f"{img_html}"
            f'<h2><a href="/sheets/{html.escape(slug)}">{html.escape(name)}</a></h2>'
            f"<p>{_inline(html.escape(blurb))}</p>"
            f"</div>"
        )

    body = (
        f"<h1>{html.escape(campaign_name)} — Characters</h1>"
        + ("".join(cards) if cards else "<p class='muted'>No characters yet.</p>")
    )
    return render(f"{campaign_name} — Characters", body)


@app.route("/locations")
def locations():
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"

    if not locs_dir.exists():
        body = f"<h1>{html.escape(campaign_name)} — Locations</h1><p class='muted'>No locations yet.</p>"
        return render(f"{campaign_name} — Locations", body)

    areas = []
    standalone = []

    for item in sorted(locs_dir.iterdir()):
        if item.is_dir():
            sub_count = sum(1 for f in item.glob("*.md"))
            area_md = locs_dir / f"{item.name}.md"
            area_name = _read_first_heading(area_md) if area_md.exists() else item.name
            areas.append((item.name, area_name, sub_count))
        elif item.is_file() and item.suffix == ".md":
            name = _read_first_heading(item)
            blurb = _read_first_paragraph(item)
            standalone.append((item.stem, name, blurb))

    left_html = ""
    if areas:
        area_items = "".join(
            f'<div class="card">'
            f'<h2><a href="/locations/{html.escape(slug)}">{html.escape(name)}</a></h2>'
            f'<p class="muted">{sub} location{"s" if sub != 1 else ""}</p>'
            f'</div>'
            for slug, name, sub in areas
        )
        left_html = f"<h2>Areas</h2>{area_items}"

    right_html = ""
    if standalone:
        loc_items = "".join(
            f'<div class="card">'
            f'<h2><a href="/locations/_/{html.escape(slug)}">{html.escape(name)}</a></h2>'
            f'<p>{html.escape(blurb)}</p>'
            f'</div>'
            for slug, name, blurb in standalone
        )
        right_html = f"<h2>Locations</h2>{loc_items}"

    if left_html or right_html:
        body = (
            f"<h1>{html.escape(campaign_name)} — Locations</h1>"
            f'<div class="two-col">'
            f'<div>{left_html}</div>'
            f'<div>{right_html}</div>'
            f'</div>'
        )
    else:
        body = f"<h1>{html.escape(campaign_name)} — Locations</h1><p class='muted'>No locations yet.</p>"

    return render(f"{campaign_name} — Locations", body)


@app.route("/locations/<area>")
def area_detail(area: str):
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"

    area_md = locs_dir / f"{area}.md"
    area_dir = locs_dir / area

    area_name = _read_first_heading(area_md) if area_md.exists() else area

    content_html = ""
    if area_md.exists():
        content_html = f'<div class="card">{_markdown_to_html(area_md.read_text(encoding="utf-8"))}</div>'

    sub_cards = []
    if area_dir.exists():
        for md in sorted(area_dir.glob("*.md")):
            name = _read_first_heading(md)
            blurb = _read_first_paragraph(md)
            slug = md.stem
            sub_cards.append(
                f'<div class="card">'
                f'<h2><a href="/locations/{html.escape(area)}/{html.escape(slug)}">{html.escape(name)}</a></h2>'
                f'<p>{html.escape(blurb)}</p>'
                f'</div>'
            )

    sub_html = ""
    if sub_cards:
        sub_html = f"<h2>Locations in {html.escape(area_name)}</h2>" + "".join(sub_cards)

    body = (
        f'<p><a href="/locations">← Locations</a></p>'
        f"<h1>{html.escape(area_name)}</h1>"
        f"{content_html}"
        f"{sub_html}"
    )
    return render(f"{html.escape(area_name)} — {html.escape(campaign_name)}", body)


@app.route("/locations/_/<slug>")
def location_standalone(slug: str):
    """Serve a top-level (non-area) location."""
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"
    path = locs_dir / f"{slug}.md"

    if not path.exists():
        abort(404)

    name = _read_first_heading(path)
    content_html = _markdown_to_html(path.read_text(encoding="utf-8"))

    body = (
        f'<p><a href="/locations">← Locations</a></p>'
        f'<div class="card">{content_html}</div>'
    )
    return render(f"{html.escape(name)} — {html.escape(campaign_name)}", body)


@app.route("/locations/<area>/<slug>")
def location_detail(area: str, slug: str):
    campaign_name = cfg.get("name", "Campaign")
    locs_dir = cfg["_data_dir"] / "locations"
    path = locs_dir / area / f"{slug}.md"

    if not path.exists():
        abort(404)

    name = _read_first_heading(path)
    area_md = locs_dir / f"{area}.md"
    area_name = _read_first_heading(area_md) if area_md.exists() else area
    content_html = _markdown_to_html(path.read_text(encoding="utf-8"))

    body = (
        f'<p><a href="/locations">← Locations</a> / '
        f'<a href="/locations/{html.escape(area)}">{html.escape(area_name)}</a></p>'
        f'<div class="card">{content_html}</div>'
    )
    return render(f"{html.escape(name)} — {html.escape(campaign_name)}", body)


@app.route("/images/<filename>")
def serve_image(filename: str):
    images_dir = cfg["_data_dir"] / "images"
    return send_from_directory(str(images_dir), filename)


# Monster routes are defined in dashboard_monsters.py and registered at the
# bottom of this module via dashboard_monsters.init(...).


# ---------------------------------------------------------------------------
# Spells
# ---------------------------------------------------------------------------

SELECT_STYLE = (
    'style="background:#2a2018;border:1px solid #4a3828;color:#d4c5a9;'
    'padding:6px 10px;border-radius:4px;font-size:1em"'
)
INPUT_STYLE = (
    'style="background:#2a2018;border:1px solid #4a3828;color:#d4c5a9;'
    'padding:6px 10px;border-radius:4px;width:220px;font-size:1em"'
)
BTN_STYLE = (
    'style="background:#3a2818;border:1px solid #5a4030;color:#c8a96e;'
    'padding:6px 14px;border-radius:4px;cursor:pointer"'
)

_WIZARD_SCHOOLS = [
    "Abjuration", "Alteration", "Conjuration/Summoning", "Divination",
    "Enchantment/Charm", "Evocation", "Illusion/Phantasm", "Invocation/Evocation", "Necromancy",
]
_PRIEST_SCHOOLS = [
    "Abjuration", "Alteration", "Conjuration", "Divination",
    "Enchantment", "Evocation", "Illusion", "Necromancy",
]
_ALL_SCHOOLS = sorted(set(_WIZARD_SCHOOLS + _PRIEST_SCHOOLS))


def _spell_query(q: str, caster: str, school: str, level: str):
    if not _2E_DB.exists():
        return []
    conn = _db(_2E_DB)
    where, params = ["1=1"], []
    if q:
        where.append("name LIKE ? COLLATE NOCASE")
        params.append(f"%{q}%")
    if caster:
        where.append("lower(caster) = ?")
        params.append(caster)
    if level and level.isdigit():
        where.append("level = ?")
        params.append(int(level))
    if school:
        where.append("school LIKE ? COLLATE NOCASE")
        params.append(f"%{school}%")
    rows = conn.execute(
        f"SELECT id, name, level, school, caster, casting_time FROM spells "
        f"WHERE {' AND '.join(where)} ORDER BY level, name",
        params,
    ).fetchall()
    return rows


def _spell_card(r) -> str:
    return (
        f'<div class="card" style="padding:8px 14px">'
        f'<h2 style="margin:0 0 2px">'
        f'<a href="/spells/{r["id"]}">{html.escape(r["name"])}</a></h2>'
        f'<p class="muted" style="margin:0;font-size:.9em">'
        f'Level&nbsp;{html.escape(str(r["level"] or "—"))} &nbsp;·&nbsp; '
        f'{html.escape(r["school"] or "—")} &nbsp;·&nbsp; '
        f'CT&nbsp;{html.escape(str(r["casting_time"] or "—"))}'
        f'</p></div>'
    )


def _spell_section(title: str, rows) -> str:
    if not rows:
        return ""
    cards = "".join(_spell_card(r) for r in rows)
    count = f"<span class='muted' style='font-size:.85em;margin-left:8px'>({len(rows)})</span>"
    return f"<h2 style='margin-top:24px'>{html.escape(title)}{count}</h2>{cards}"


def _spell_results_html(rows, caster: str) -> str:
    if not rows:
        return "<p class='muted'>No results.</p>"
    if not caster:
        wizard_rows = [r for r in rows if (r["caster"] or "").lower() == "wizard"]
        priest_rows = [r for r in rows if (r["caster"] or "").lower() == "priest"]
        other_rows  = [r for r in rows if (r["caster"] or "").lower() not in ("wizard", "priest")]
        count = f"<p class='muted' style='margin-bottom:4px'>{len(rows)} spell{'s' if len(rows) != 1 else ''}</p>"
        return (count
                + _spell_section("Wizard Spells", wizard_rows)
                + _spell_section("Priest Spells", priest_rows)
                + _spell_section("Other", other_rows))
    count = f"<p class='muted' style='margin-bottom:4px'>{len(rows)} spell{'s' if len(rows) != 1 else ''}</p>"
    return count + "".join(_spell_card(r) for r in rows)


@app.route("/api/spells/cards")
def api_spell_cards():
    q      = request.args.get("q", "").strip()
    caster = request.args.get("caster", "").strip().lower()
    school = request.args.get("school", "").strip()
    level  = request.args.get("level", "").strip()
    rows   = _spell_query(q, caster, school, level)
    return Response(_spell_results_html(rows, caster), mimetype="text/html")


_SPELLS_JS = r"""
<script>
const WIZARD_SCHOOLS = """ + json.dumps(_WIZARD_SCHOOLS) + r""";
const PRIEST_SCHOOLS = """ + json.dumps(_PRIEST_SCHOOLS) + r""";
const ALL_SCHOOLS    = """ + json.dumps(_ALL_SCHOOLS) + r""";

let _timer = null;

function schoolsFor(caster) {
  if (caster === "wizard") return WIZARD_SCHOOLS;
  if (caster === "priest") return PRIEST_SCHOOLS;
  return ALL_SCHOOLS;
}

function updateSchools() {
  const caster = document.querySelector('input[name="caster"]:checked').value;
  const sel = document.getElementById("school-sel");
  const prev = sel.value;
  sel.innerHTML = '<option value="">All schools</option>';
  for (const s of schoolsFor(caster)) {
    const opt = document.createElement("option");
    opt.value = s; opt.textContent = s;
    if (s === prev) opt.selected = true;
    sel.appendChild(opt);
  }
}

async function doFetch() {
  const q      = document.getElementById("spell-q").value.trim();
  const caster = document.querySelector('input[name="caster"]:checked').value;
  const school = document.getElementById("school-sel").value;
  const level  = document.getElementById("level-sel").value;
  if (q.length > 0 && q.length < 3) return;
  const p = new URLSearchParams();
  if (q)      p.set("q",      q);
  if (caster) p.set("caster", caster);
  if (school) p.set("school", school);
  if (level)  p.set("level",  level);
  const r = await fetch("/api/spells/cards?" + p.toString());
  document.getElementById("spell-results").innerHTML = await r.text();
}

function schedule() {
  clearTimeout(_timer);
  _timer = setTimeout(doFetch, 280);
}

document.getElementById("spell-q").addEventListener("input", schedule);
document.getElementById("school-sel").addEventListener("change", schedule);
document.getElementById("level-sel").addEventListener("change", schedule);
document.querySelectorAll('input[name="caster"]').forEach(r => {
  r.addEventListener("change", () => { updateSchools(); schedule(); });
});
</script>
"""


@app.route("/spells")
def spells():
    q      = request.args.get("q", "").strip()
    caster = request.args.get("caster", "").strip().lower()
    level  = request.args.get("level", "").strip()
    school = request.args.get("school", "").strip()

    if not _2E_DB.exists():
        body = "<h1>Spells</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
        return render("Spells", body)

    rows = _spell_query(q, caster, school, level)

    def _radio(val, label):
        chk = "checked" if caster == val else ""
        return (
            f'<label style="display:inline-flex;align-items:center;gap:4px;'
            f'cursor:pointer;color:#d4c5a9;font-size:.95em">'
            f'<input type="radio" name="caster" value="{val}" {chk} '
            f'style="accent-color:#c8a96e"> {label}</label>'
        )

    radios = (
        f'<div style="display:flex;gap:14px;align-items:center;'
        f'background:#2a2018;border:1px solid #4a3828;border-radius:4px;padding:6px 12px">'
        + _radio("", "Both") + _radio("wizard", "Wizard") + _radio("priest", "Priest")
        + '</div>'
    )

    schools_for_caster = (
        _WIZARD_SCHOOLS if caster == "wizard"
        else _PRIEST_SCHOOLS if caster == "priest"
        else _ALL_SCHOOLS
    )
    school_opts = '<option value="">All schools</option>' + "".join(
        f'<option value="{s}" {"selected" if school == s else ""}>{s}</option>'
        for s in schools_for_caster
    )
    level_opts = '<option value="">All levels</option>' + "".join(
        f'<option value="{i}" {"selected" if level == str(i) else ""}>Level {i}</option>'
        for i in range(1, 10)
    )

    controls = (
        f'<div style="margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'<input id="spell-q" name="q" value="{html.escape(q)}" placeholder="Search by name…" {INPUT_STYLE} autocomplete="off">'
        f'{radios}'
        f'<select id="school-sel" name="school" {SELECT_STYLE}>{school_opts}</select>'
        f'<select id="level-sel" name="level" {SELECT_STYLE}>{level_opts}</select>'
        f'</div>'
    )

    body = (
        f"<h1>Spells</h1>{controls}"
        f'<div id="spell-results">{_spell_results_html(rows, caster)}</div>'
        + _SPELLS_JS
    )
    return render("Spells", body)


@app.route("/spells/<int:spell_id>")
def spell_detail(spell_id: int):
    if not _2E_DB.exists():
        abort(404)

    conn = _db(_2E_DB)
    row = conn.execute("SELECT * FROM spells WHERE id=?", (spell_id,)).fetchone()

    if row is None:
        abort(404)

    def stat(label, val):
        if not val and val != 0:
            return ""
        return (
            f'<tr><th style="width:160px;white-space:nowrap">{html.escape(label)}</th>'
            f'<td>{html.escape(str(val))}</td></tr>'
        )

    components = []
    if row["verbal"]:  components.append("V")
    if row["somatic"]: components.append("S")
    if row["material"]:components.append("M")
    comp_str = ", ".join(components) or "—"
    if row["materials"]:
        comp_str += f" ({html.escape(row['materials'])})"

    table = (
        "<table>"
        + stat("Caster",       (row["caster"] or "").capitalize())
        + stat("Level",        row["level"])
        + stat("School",       row["school"])
        + stat("Casting Time", row["casting_time"])
        + stat("Range",        row["range"])
        + stat("Area of Effect", row["aoe"])
        + stat("Duration",     row["duration"])
        + stat("Saving Throw", row["save"])
        + stat("Damage",       row["damage"])
        + f'<tr><th>Components</th><td>{comp_str}</td></tr>'
        + stat("Reversible",   "Yes" if row["reversible"] else None)
        + stat("Source",       f"{row['source']} p.{row['page']}" if row["source"] else None)
        + "</table>"
    )

    desc_html = _markdown_to_html(row["description"]) if row["description"] else ""

    body = (
        f'<p><a href="/spells">← Spells</a></p>'
        f'<h1>{html.escape(row["name"])}</h1>'
        f'<div class="card">{table}</div>'
        + (f'<div class="card" style="margin-top:12px">{desc_html}</div>' if desc_html else "")
    )
    return render(f'{html.escape(row["name"])} — Spells', body)


# ---------------------------------------------------------------------------
# Classes
# ---------------------------------------------------------------------------

@app.route("/classes")
def classes():
    if not _2E_DB.exists():
        body = "<h1>Classes</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
        return render("Classes", body)

    conn = _db(_2E_DB)
    rows = conn.execute("SELECT id, name, hit_die, prime_requisite FROM classes ORDER BY name").fetchall()

    cards = []
    for r in rows:
        slug = re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
        try:
            pr = json.loads(r["prime_requisite"] or "[]")
            pr_str = ", ".join(pr).capitalize() if pr else "—"
        except (json.JSONDecodeError, TypeError):
            pr_str = str(r["prime_requisite"] or "—")
        cards.append(
            f'<div class="card" style="padding:10px 14px">'
            f'<h2 style="margin:0 0 2px">'
            f'<a href="/classes/{html.escape(slug)}">{html.escape(r["name"])}</a></h2>'
            f'<p class="muted" style="margin:0;font-size:.9em">'
            f'Hit Die: {html.escape(r["hit_die"] or "—")} &nbsp;·&nbsp; '
            f'Prime Requisite: {html.escape(pr_str)}'
            f'</p></div>'
        )

    body = "<h1>Classes</h1>" + "".join(cards)
    return render("Classes", body)


@app.route("/classes/<slug>")
def class_detail(slug: str):
    if not _2E_DB.exists():
        abort(404)

    name_pattern = slug.replace("-", " ")
    conn = _db(_2E_DB)
    cls_row = conn.execute(
        "SELECT * FROM classes WHERE lower(replace(name,' ','-')) = ? "
        "OR name LIKE ? COLLATE NOCASE LIMIT 1",
        (slug, f"%{name_pattern}%"),
    ).fetchone()

    if cls_row is None:
        abort(404)

    level_rows = conn.execute(
        "SELECT * FROM class_levels WHERE class_id=? ORDER BY level",
        (cls_row["id"],),
    ).fetchall()

    try:
        pr = json.loads(cls_row["prime_requisite"] or "[]")
        pr_str = ", ".join(str(p).capitalize() for p in pr) if pr else "—"
    except (json.JSONDecodeError, TypeError):
        pr_str = str(cls_row["prime_requisite"] or "—")

    try:
        abilities = json.loads(cls_row["special_abilities"] or "[]")
    except (json.JSONDecodeError, TypeError):
        abilities = []

    # Build XP / progression table
    has_spells    = any(r["spell_slots"] for r in level_rows)
    has_turn      = any(r["turn_undead"] for r in level_rows)

    header_cells = "<tr><th>Level</th><th>XP</th><th>HD</th><th>THAC0</th>"
    header_cells += "<th>Para</th><th>RSW</th><th>Pet</th><th>Brth</th><th>Spell</th>"
    if has_spells:
        header_cells += "<th>Spell Slots</th>"
    header_cells += "</tr>"

    tbody = ""
    for r in level_rows:
        slots_str = ""
        if has_spells and r["spell_slots"]:
            try:
                slots = json.loads(r["spell_slots"])
                slots_str = " / ".join(str(v) for k, v in sorted(slots.items()) if v)
            except Exception:
                slots_str = "—"

        xp = f"{r['xp_required']:,}" if r["xp_required"] is not None else "—"
        tbody += (
            f"<tr>"
            f"<td>{r['level']}</td>"
            f"<td>{xp}</td>"
            f"<td>{html.escape(r['hit_dice'] or '—')}</td>"
            f"<td>{r['thac0'] or '—'}</td>"
            f"<td>{r['save_paralysis'] or '—'}</td>"
            f"<td>{r['save_rsw'] or '—'}</td>"
            f"<td>{r['save_petrify'] or '—'}</td>"
            f"<td>{r['save_breath'] or '—'}</td>"
            f"<td>{r['save_spell'] or '—'}</td>"
            + (f"<td>{slots_str or '—'}</td>" if has_spells else "")
            + "</tr>"
        )

    prog_table = f"<table><thead>{header_cells}</thead><tbody>{tbody}</tbody></table>"

    abilities_html = ""
    if abilities:
        items = "".join(f"<li>{html.escape(str(a))}</li>" for a in abilities)
        abilities_html = f"<h2>Special Abilities</h2><ul>{items}</ul>"

    desc_html = ""
    if cls_row["description"]:
        paras = [p.strip() for p in cls_row["description"].split("\n") if p.strip()]
        desc_html = "".join(f"<p>{html.escape(p)}</p>" for p in paras[:4])
        if len(paras) > 4:
            desc_html += f"<p class='muted'>…{len(paras)-4} more paragraphs</p>"

    body = (
        f'<p><a href="/classes">← Classes</a></p>'
        f'<h1>{html.escape(cls_row["name"])}</h1>'
        f'<div class="card">'
        f'<p><strong>Hit Die:</strong> {html.escape(cls_row["hit_die"] or "—")} &nbsp;·&nbsp; '
        f'<strong>Prime Requisite:</strong> {html.escape(pr_str)}</p>'
        + (f"<hr>{desc_html}" if desc_html else "")
        + f'</div>'
        f'<h2 style="margin-top:20px">Progression</h2>'
        f'<div class="card" style="overflow-x:auto">{prog_table}</div>'
        + (f'<div class="card">{abilities_html}</div>' if abilities_html else "")
    )
    return render(f'{html.escape(cls_row["name"])} — Classes', body)


# Item routes are defined in dashboard_items.py and registered at the end of
# this module via dashboard_items.init(...) — keeps this file shorter without
# changing any behaviour.


# ---------------------------------------------------------------------------
# Combat map
# ---------------------------------------------------------------------------

_COMBAT_CSS = """
<style>
.container{max-width:1400px}
.combat-layout{display:flex;gap:20px;align-items:flex-start;flex-wrap:nowrap}
.combat-canvas-wrap{flex:1 1 auto;overflow:hidden;min-width:0;height:560px;
  border:1px solid #4a3828;border-radius:4px}
#cmap{display:block;cursor:grab;outline:none}
.combat-sidebar{flex:0 0 210px;width:210px;flex-shrink:0}
.zoom-btn{background:#2a2018;border:1px solid #4a3828;color:#c8a96e;padding:2px 9px;
  border-radius:3px;cursor:pointer;font-size:1em;line-height:1.4}
.zoom-btn:hover{background:#3a3020}
.init-row{display:flex;align-items:center;gap:8px;padding:5px 8px;border-radius:4px;
  margin:2px 0;border-left:3px solid transparent}
.init-row.party{border-left-color:#4a8c4a}
.init-row.enemy{border-left-color:#8c2a2a}
.init-current{background:#2a2218}
.init-portrait{width:34px;height:34px;border-radius:50%;overflow:hidden;flex-shrink:0;
  background:#3a2818;display:flex;align-items:center;justify-content:center;
  color:#d4c5a9;font-weight:bold;font-size:14px;border:2px solid #666}
.init-portrait img{width:100%;height:100%;object-fit:cover}
.init-info{flex:1;display:flex;justify-content:space-between;align-items:center;min-width:0}
.init-name{font-size:.88em;color:#d4c5a9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.init-current .init-name{color:#c8a96e;font-weight:bold}
.init-val{color:#8a7a60;font-size:.85em;font-family:monospace;margin-left:6px;flex-shrink:0}
</style>
"""

_COMBAT_JS = (
    'var map = new CombatMap(document.getElementById("cmap"), {cellPx: 36});\n'
    'var initListEl = document.getElementById("init-list");\n'
    '\n'
    'map.onZoom = function(z) {\n'
    '  var el = document.getElementById("zoom-label");\n'
    '  if (el) el.textContent = Math.round(z*100)+"%";\n'
    '};\n'
    '\n'
    'map.onHover = function(x, y, label) {\n'
    '  var txt = "("+x+", "+y+")";\n'
    '  if (label) txt += " — " + label;\n'
    '  else txt += " — click to copy";\n'
    '  document.getElementById("coords").textContent = txt;\n'
    '};\n'
    '\n'
    'map.onTokenClick = function(c) {\n'
    '  var el = document.getElementById("coords");\n'
    '  if (c) el.textContent = c.name + " — movement: " + (c.movement||6) + " cells (click again to deselect)";\n'
    '  else el.textContent = "hover map for coordinates · scroll to zoom";\n'
    '};\n'
    '\n'
    'map.onClick = function(x, y) {\n'
    '  document.getElementById("coords").textContent = \'place_combatant("name", \'+x+", "+y+")";\n'
    '};\n'
    '\n'
    'async function refresh() {\n'
    '  try {\n'
    '    var r = await fetch("/combat-state");\n'
    '    var data = await r.json();\n'
    '    if (!data.active || !data.grid) {\n'
    '      document.getElementById("round-info").textContent = "No active combat map";\n'
    '      document.getElementById("turn-info").textContent = "";\n'
    '      initListEl.innerHTML = \'<p class="muted" style="font-size:.85em">No combat active</p>\';\n'
    '      return;\n'
    '    }\n'
    '    document.getElementById("round-info").textContent = "Round " + data.round;\n'
    '    document.getElementById("turn-info").textContent = "▶ " + (data.current || "");\n'
    '    map.render(data);\n'
    '    map.renderInitList(initListEl, data.combatants || []);\n'
    '    var unplaced = (data.combatants || []).filter(function(c){\n'
    '      return c.x === null || c.x === undefined;\n'
    '    });\n'
    '    document.getElementById("unplaced").innerHTML = unplaced.length\n'
    '      ? \'<span style="color:#8a7a60;font-size:.85em">Unplaced: \'\n'
    '        + unplaced.map(function(c){ return c.name; }).join(", ")\n'
    '        + \'</span>\'\n'
    '      : "";\n'
    '  } catch(e) {\n'
    '    document.getElementById("round-info").textContent = "Waiting for combat…";\n'
    '  }\n'
    '}\n'
    'refresh();\n'
    'setInterval(refresh, 2000);\n'
)


@app.route("/combat")
def combat_view():
    name = html.escape(cfg.get("name", "Campaign"))
    body = (
        _COMBAT_CSS
        + f'<h1>Combat Map</h1>'
        + '<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px">'
        + '  <span id="round-info" style="color:#c8a96e;font-weight:bold">Loading…</span>'
        + '  <span id="turn-info" style="color:#d4c5a9"></span>'
        + '  <span style="margin-left:auto;display:flex;gap:5px;align-items:center">'
        + '    <button class="zoom-btn" onclick="map.setZoom(map.zoom*1.25)">+</button>'
        + '    <button class="zoom-btn" onclick="map.setZoom(map.zoom*0.8)">−</button>'
        + '    <button class="zoom-btn" onclick="map.setZoom(1)" title="Reset zoom">□</button>'
        + '    <span id="zoom-label" style="color:#8a7a60;font-size:.8em;min-width:36px;text-align:right">100%</span>'
        + '  </span>'
        + '</div>'
        + '<div style="color:#8a7a60;font-size:.8em;margin-bottom:8px">'
        + '  <span id="coords">hover map for coordinates · scroll to zoom</span>'
        + '</div>'
        + '<div class="combat-layout">'
        + '  <div class="combat-sidebar">'
        + '    <h3 style="margin:0 0 8px;font-size:.95em;color:#c8a96e">Initiative Order</h3>'
        + '    <div id="init-list"></div>'
        + '  </div>'
        + '  <div class="combat-canvas-wrap">'
        + '    <canvas id="cmap"></canvas>'
        + '    <div id="unplaced" style="margin-top:6px"></div>'
        + '  </div>'
        + '</div>'
        + '<script src="/static/combat-map.js"></script>'
        + '<script>' + _COMBAT_JS + '</script>'
    )
    return render(f"Combat — {name}", body)


@app.route("/combat-state")
def combat_state():
    cs = cfg.get("_dir", Path(".")) / "combat_state.json"
    if cs.exists():
        try:
            data = json.loads(cs.read_text(encoding="utf-8"))
            return jsonify(data)
        except Exception:
            pass
    return jsonify({"active": False})


# ---------------------------------------------------------------------------
# Sub-module route registration
# ---------------------------------------------------------------------------
import dashboard_items as _dashboard_items
import dashboard_monsters as _dashboard_monsters

_dashboard_items.init(
    app,
    render=render,
    db_get=_db,
    _2E_DB=_2E_DB,
    markdown_to_html=_markdown_to_html,
    INPUT_STYLE=INPUT_STYLE,
    SELECT_STYLE=SELECT_STYLE,
)

_dashboard_monsters.init(
    app,
    get_cfg=lambda: cfg,
    render=render,
    db_get=_db,
    _MONSTERS_DB=_MONSTERS_DB,
    _MONSTERS_DIR=_MONSTERS_DIR,
    monster_portraits_dict=_monster_portraits_dict,
    monster_portrait_path=_monster_portrait_path,
)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Campaign dashboard web app."
    )
    parser.add_argument("--campaign", help="Campaign name (default: active campaign)")
    parser.add_argument("--port", type=int, default=5000, help="Port to listen on (default: 5000)")
    args = parser.parse_args()

    try:
        cfg = _c.load_campaign(args.campaign or None)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error loading campaign: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Campaign: {cfg.get('name', cfg['_name'])}")
    print(f"Data dir: {cfg['_data_dir']}")
    print(f"Starting dashboard at http://localhost:{args.port}")
    app.run(debug=True, port=args.port)
