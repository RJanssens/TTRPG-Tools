#!/usr/bin/env python3
"""Campaign dashboard web app. Run: python3 dashboard.py [--campaign NAME] [--port 5000]"""

import argparse
import html
import json
import re
import sqlite3
import sys
from pathlib import Path

from datetime import datetime, timezone

from flask import Flask, Response, abort, jsonify, render_template_string, request, send_from_directory

import _campaign as _c
from tools import export_session as _export
from tools import item_weights as _iw
from tools import homebrew_classes as _hb
from tools import dm_session as _dm
from tools import images as _img

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


def _combat_hp_overrides() -> dict:
    """If a combat session is active, return a dict mapping party-character
    keys to live HP from combat_state.json so /party (and the status line)
    show current HP during combat instead of stale state.json values."""
    if not cfg:
        return {}
    cs_path = cfg["_dir"] / "combat_state.json"
    if not cs_path.exists():
        return {}
    try:
        cs = json.loads(cs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not cs.get("active"):
        return {}
    out: dict = {}
    for c in cs.get("combatants", []):
        if c.get("side") != "party":
            continue
        key = c.get("key")
        if key:
            out[key] = c.get("hp")
    return out


def _combat_effects_overrides() -> dict:
    """Return {character_key: [effect_dict, ...]} for party members in an
    active combat session. Empty dict if no combat is active."""
    if not cfg:
        return {}
    cs_path = cfg["_dir"] / "combat_state.json"
    if not cs_path.exists():
        return {}
    try:
        cs = json.loads(cs_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not cs.get("active"):
        return {}
    out: dict = {}
    for c in cs.get("combatants", []):
        if c.get("side") != "party":
            continue
        key = c.get("key")
        effects = c.get("effects") or []
        if key and effects:
            out[key] = effects
    return out


_class_xp_cache: dict[str, list[int]] = {}

# 2e kits and common synonyms map onto their PHB base class for XP-table
# lookup. The kits use the base class's progression in the published rules.
_CLASS_ALIASES: dict[str, str] = {
    "barbarian":   "Fighter",   # Complete Barbarian's Handbook (Fighter kit)
    "berserker":   "Fighter",
    "cavalier":    "Fighter",
    "gladiator":   "Fighter",
    "samurai":     "Fighter",
    "wizard":      "Mage",
    "necromancer": "Specialist Mage",
    "abjurer":     "Specialist Mage",
    "conjurer":    "Specialist Mage",
    "diviner":     "Specialist Mage",
    "enchanter":   "Specialist Mage",
    "illusionist": "Specialist Mage",
    "invoker":     "Specialist Mage",
    "transmuter":  "Specialist Mage",
    "monk":        "Cleric",    # Scarlet Brotherhood / OA-style monk uses Priest table
    "assassin":    "Thief",     # 2e: Assassin kit of Thief
    "swashbuckler":"Thief",
}


def _class_xp_table(cls: str) -> list[int]:
    """Return [xp_required at level 1, level 2, ...] for a class.

    Resolution order:
      1. Homebrew classes (global/homebrew/classes/*.json) — preferred so
         home-brewed entries win over aliases.
      2. PHB classes (global/2e.db).
      3. Kit→base alias fallback (legacy; will be empty for classes whose
         homebrew JSON has been authored).
    Empty list if nothing matches. Process-cached."""
    key = (cls or "").strip().lower()
    if not key:
        return []
    if key in _class_xp_cache:
        return _class_xp_cache[key]

    table: list[int] = _hb.homebrew_xp_table(key)
    if not table:
        candidates = [key]
        if key in _CLASS_ALIASES:
            candidates.append(_CLASS_ALIASES[key].lower())
        try:
            conn = _db(_2E_DB)
            for candidate in candidates:
                row = conn.execute(
                    "SELECT id FROM classes WHERE lower(name) = ? LIMIT 1", (candidate,),
                ).fetchone()
                if row is None:
                    row = conn.execute(
                        "SELECT id FROM classes WHERE lower(name) LIKE ? LIMIT 1",
                        (candidate + "%",),
                    ).fetchone()
                if row is not None:
                    for r in conn.execute(
                        "SELECT xp_required FROM class_levels WHERE class_id=? ORDER BY level",
                        (row["id"],),
                    ).fetchall():
                        table.append(int(r["xp_required"]))
                    break
        except sqlite3.Error:
            table = []

    _class_xp_cache[key] = table
    return table


# Encumbrance bands (PHB Table 47) and computation live in tools/item_weights.py.
# These thin wrappers inject the dashboard's cached sqlite connection.
def _item_weight_resolve(name: str) -> dict:
    return _iw.resolve_weight(name, conn=_db(_2E_DB))


def _item_weight_lookup(name: str) -> float:
    return _item_weight_resolve(name)["lb"]


def _encumbrance_band(char: dict, state: dict, key: str) -> dict:
    """Compute encumbrance for a character. See item_weights.compute_encumbrance."""
    return _iw.compute_encumbrance(char, state, key, conn=_db(_2E_DB))


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
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@400;500;600&family=EB+Garamond:ital,wght@0,400;0,500;0,600;1,400;1,500&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg-base:        #14100b;
    --bg-deep:        #0a0805;
    --bg-card:        #221a12;
    --bg-card-hi:     #2a2018;
    --bg-rec:         #0f0b07;
    --bg-nav:         rgba(13, 10, 7, 0.92);
    --ink-body:       #e8d8b8;
    --ink-display:    #f0d9a4;
    --ink-muted:      #8e7e5e;
    --accent-gold:    #c8a96e;
    --accent-gold-hi: #f0d196;
    --accent-rust:    #b56a3a;
    --accent-blood:   #c83a2a;
    --accent-cool:    #6a9ec0;
    --rule:           #3d2c1e;
    --rule-hi:        #5a4030;
    --hp-good:        #7ab46e;
    --hp-warn:        #d4a14a;
    --hp-bad:         #c8503a;
    --shadow-deep:    0 10px 30px rgba(0,0,0,.55);
    --shadow-card:    0 2px 10px rgba(0,0,0,.30);
    --inset-warm:     inset 0 1px 0 rgba(220,180,120,0.05);
    --font-display:   'Cinzel', 'Trajan Pro', Georgia, serif;
    --font-body:      'EB Garamond', Georgia, 'Times New Roman', serif;
    --font-mono:      'JetBrains Mono', Menlo, Consolas, monospace;
  }}

  * {{ box-sizing: border-box; margin: 0; padding: 0; }}

  html {{ scrollbar-color: var(--rule-hi) var(--bg-deep); }}
  ::selection {{ background: rgba(200,169,110,0.35); color: var(--ink-display); }}

  body {{
    background: var(--bg-base);
    background-image:
      radial-gradient(ellipse at 18% -10%, rgba(120,80,40,0.16), transparent 55%),
      radial-gradient(ellipse at 85% 105%, rgba(80,50,25,0.14), transparent 50%),
      linear-gradient(180deg, #15110c 0%, #100c08 100%);
    background-attachment: fixed;
    color: var(--ink-body);
    font-family: var(--font-body);
    font-size: 18px;
    line-height: 1.62;
    font-feature-settings: "liga", "kern", "onum";
    text-rendering: optimizeLegibility;
    -webkit-font-smoothing: antialiased;
    min-height: 100vh;
  }}

  /* Subtle parchment grain over the entire viewport. */
  body::before {{
    content: '';
    position: fixed; inset: 0;
    pointer-events: none;
    z-index: 1;
    opacity: 0.045;
    background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='240' height='240'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='2' seed='4'/><feColorMatrix values='0 0 0 0 0  0 0 0 0 0  0 0 0 0 0  0.6 0.6 0.6 0 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  }}

  .container {{
    max-width: 980px;
    margin: 0 auto;
    padding: 28px 24px 60px;
    position: relative;
    z-index: 2;
  }}

  /* ---- Navigation ---- */
  nav {{
    background: var(--bg-nav);
    border-bottom: 1px solid var(--rule);
    box-shadow: 0 1px 0 rgba(200,169,110,0.04), 0 4px 16px rgba(0,0,0,0.4);
    padding: 14px 28px;
    position: sticky; top: 0;
    backdrop-filter: blur(8px);
    -webkit-backdrop-filter: blur(8px);
    z-index: 50;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
  }}
  nav a {{
    color: var(--accent-gold);
    text-decoration: none;
    font-family: var(--font-display);
    font-size: 0.72em;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.22em;
    padding: 4px 16px;
    position: relative;
    transition: color 180ms ease;
  }}
  nav a:not(:last-child)::after {{
    content: '';
    position: absolute;
    right: 0; top: 28%; bottom: 28%;
    width: 1px;
    background: linear-gradient(to bottom, transparent, rgba(200,169,110,0.24), transparent);
  }}
  nav a:hover {{ color: var(--accent-gold-hi); }}
  nav a.is-active {{ color: var(--ink-display); }}
  nav a.is-active::before {{
    content: '';
    position: absolute;
    left: 14px; right: 14px; bottom: -16px;
    height: 2px;
    background: var(--accent-gold);
  }}

  /* ---- Typography ---- */
  h1, h2, h3, h4 {{
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-weight: 500;
    letter-spacing: 0.04em;
    margin: 22px 0 10px;
  }}
  h1 {{
    font-size: 2.05em;
    border-bottom: 1px solid var(--rule);
    padding-bottom: 12px;
    margin-bottom: 18px;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    color: var(--ink-display);
  }}
  h2 {{ font-size: 1.32em; letter-spacing: 0.06em; }}
  h3 {{ font-size: 1.10em; letter-spacing: 0.05em; color: var(--accent-gold); }}
  h4 {{ font-size: 0.98em; letter-spacing: 0.08em; text-transform: uppercase; color: var(--accent-gold); }}
  p {{ margin: 8px 0; }}
  ul, ol {{ margin: 8px 0 8px 24px; }}
  a {{ color: var(--accent-gold); transition: color 160ms ease; }}
  a:hover {{ color: var(--accent-gold-hi); }}

  /* ---- Cards & layout ---- */
  .card {{
    background:
      linear-gradient(135deg, rgba(255,220,180,0.020), transparent 45%),
      linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 18px 22px;
    margin: 12px 0;
    box-shadow: var(--inset-warm), var(--shadow-card);
    position: relative;
  }}
  .card h2 {{ margin-top: 0; }}
  .grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
    gap: 18px;
    margin-top: 16px;
  }}
  .grid-item {{
    background: var(--bg-card);
    border: 1px solid var(--rule);
    border-radius: 4px;
    overflow: hidden;
    transition: border-color 200ms ease, transform 200ms ease;
  }}
  .grid-item:hover {{ border-color: var(--rule-hi); transform: translateY(-2px); }}
  .grid-item img {{ width: 100%; display: block; border-radius: 3px 3px 0 0; }}
  .grid-item .caption {{ padding: 12px 14px; font-size: 0.92em; color: var(--ink-muted); }}
  .grid-item .caption strong {{ display: block; color: var(--ink-body); margin-bottom: 4px; font-family: var(--font-display); font-size: 0.95em; letter-spacing: 0.04em; }}

  /* ---- HP bar (legacy text-block form, kept for back-compat) ---- */
  .hp-bar {{
    display: inline-block;
    font-family: var(--font-mono);
    letter-spacing: 1px;
    font-size: 0.88em;
  }}
  .hp-bar .filled {{ color: var(--hp-good); }}
  .hp-bar .empty  {{ color: #4a2a2a; }}

  /* ---- HP meter (graphical, used by /play sidebar and elsewhere) ---- */
  .hp-meter {{
    display: block;
    height: 8px;
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 1px;
    overflow: hidden;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.5);
    position: relative;
  }}
  .hp-meter > i {{
    display: block;
    height: 100%;
    background: linear-gradient(to right, var(--hp-bad) 0%, var(--hp-warn) 45%, var(--hp-good) 75%);
    transition: width 380ms ease;
  }}

  /* ---- Info bar (e.g. /party header strip) ---- */
  .info-bar {{
    background: linear-gradient(to bottom, #1d160f, #16110b);
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 12px 16px;
    margin: 0 0 18px 0;
    display: flex;
    flex-wrap: wrap;
    gap: 14px 26px;
    font-size: 0.96em;
    box-shadow: var(--inset-warm);
  }}
  .info-bar .item strong {{ color: var(--accent-gold); margin-right: 6px; font-family: var(--font-display); font-size: 0.85em; text-transform: uppercase; letter-spacing: 0.1em; }}

  /* ---- Clocks (faction timers) ---- */
  .clock {{
    display: inline-block;
    padding: 4px 11px;
    border-radius: 2px;
    border: 1px solid var(--rule);
    background: var(--bg-card);
    font-size: 0.9em;
    margin: 2px 4px 2px 0;
    font-family: var(--font-display);
    letter-spacing: 0.05em;
  }}
  .clock.urgent  {{ border-color: var(--accent-rust); color: #e8b487; background: #2e1d12; }}
  .clock.expired {{ border-color: #6a2818; color: #ff7a5a; background: #2e1410; }}

  /* ---- Truth/secret indicators (DM views) ---- */
  .truth-true        {{ color: var(--hp-good); font-size: 0.8em; }}
  .truth-partly_true {{ color: var(--accent-gold); font-size: 0.8em; }}
  .truth-false       {{ color: #c87a7a; font-size: 0.8em; }}
  .truth-fabrication {{ color: #ff5a4a; font-size: 0.8em; font-weight: 600; }}
  .secret-card {{
    background: linear-gradient(to bottom, #2a1818, #221212);
    border: 1px solid #6a3828;
    border-left: 4px solid var(--accent-blood);
    padding: 14px 18px;
    margin: 10px 0;
    border-radius: 3px;
    box-shadow: var(--shadow-card);
  }}
  .scope-warning {{ color: #e8b487; font-size: 0.85em; }}

  /* ---- Tags / chips ---- */
  .tag {{
    display: inline-block;
    background: #322012;
    border: 1px solid var(--rule-hi);
    border-radius: 2px;
    padding: 1px 8px;
    font-size: 0.78em;
    margin: 2px;
    color: var(--accent-gold);
    font-family: var(--font-display);
    letter-spacing: 0.06em;
  }}

  /* ---- Layout helpers ---- */
  .two-col {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; }}
  @media (max-width: 600px) {{ .two-col {{ grid-template-columns: 1fr; }} }}

  /* ---- Portrait (inline within prose) ---- */
  .portrait {{
    float: right;
    margin: 0 0 14px 18px;
    max-width: 180px;
    border-radius: 3px;
    border: 1px solid var(--rule-hi);
    box-shadow: var(--shadow-card);
  }}

  /* ---- Party card (used on /party page) ---- */
  .party-card {{ display: flex; gap: 18px; align-items: flex-start; }}
  .party-portrait {{
    width: 116px; height: 116px;
    object-fit: cover;
    border-radius: 3px;
    border: 1px solid var(--rule-hi);
    flex-shrink: 0;
    box-shadow: var(--shadow-card), inset 0 0 0 1px rgba(0,0,0,0.4);
  }}
  .party-body {{ flex: 1; min-width: 0; }}
  .party-body h2 {{ margin-top: 0; }}

  /* ---- Spells ---- */
  .spell-ready {{ color: var(--ink-body); }}
  .spell-cast  {{ color: #6a5a48; text-decoration: line-through; text-decoration-color: rgba(106,90,72,0.5); }}
  .memorized {{ margin: 8px 0; }}
  .spell-level {{ margin: 6px 0; }}
  .spell-level-head {{
    font-size: 0.82em;
    color: var(--accent-gold);
    margin: 4px 0 4px;
    border-bottom: 1px dotted var(--rule);
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.12em;
    padding-bottom: 3px;
  }}
  .spell-list {{ list-style: disc; margin: 2px 0 4px 22px; padding: 0; }}
  .spell-list li {{ margin: 1px 0; line-height: 1.42; }}

  /* ---- Effect / light chips ---- */
  .effect-chip {{
    display: inline-block;
    background: #182734;
    border: 1px solid #3a5a7a;
    border-radius: 2px;
    padding: 2px 8px;
    margin: 2px 2px 2px 0;
    font-size: 0.85em;
    color: #a0c0e0;
    font-family: var(--font-display);
    letter-spacing: 0.04em;
  }}
  .light-chip {{
    display: inline-block;
    background: #322014;
    border: 1px solid #6a4828;
    border-radius: 2px;
    padding: 2px 8px;
    margin: 2px 2px 2px 0;
    font-size: 0.85em;
    color: #e8c080;
    font-family: var(--font-display);
    letter-spacing: 0.04em;
  }}
  .light-chip.guttering {{
    background: #321810;
    border-color: var(--accent-rust);
    color: #ff8a6a;
    animation: flicker 1.4s ease-in-out infinite;
  }}
  @keyframes flicker {{
    0%, 100% {{ opacity: 1; }}
    47% {{ opacity: 0.78; }}
    52% {{ opacity: 1; }}
    78% {{ opacity: 0.86; }}
  }}

  /* ---- Encumbrance widgets ---- */
  .enc-card {{ margin-top: 16px; }}
  .enc-bar {{
    height: 10px;
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 1px;
    overflow: hidden;
    margin: 6px 0 10px;
    box-shadow: inset 0 1px 2px rgba(0,0,0,0.5);
  }}
  .enc-fill {{ height: 100%; transition: width .35s ease; }}
  .enc-bands {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px; }}
  .enc-band {{
    background: var(--bg-rec);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 7px 8px;
    font-size: 0.85em;
    text-align: center;
  }}
  .enc-band-name {{
    display: block;
    color: var(--ink-muted);
    text-transform: uppercase;
    font-size: 0.78em;
    font-family: var(--font-display);
    letter-spacing: 0.1em;
  }}
  .enc-band-cap {{ display: block; color: var(--ink-body); font-family: var(--font-mono); font-size: 0.92em; }}
  .enc-band-active {{ border-color: var(--accent-gold); background: var(--bg-card); }}
  .enc-band-active .enc-band-name {{ color: var(--accent-gold); }}
  .enc-items {{ margin: 12px 0 4px; font-size: 0.92em; }}
  .enc-items th {{ font-weight: 500; color: var(--accent-gold); font-family: var(--font-display); text-transform: uppercase; letter-spacing: 0.08em; font-size: 0.82em; }}
  .enc-items td, .enc-items th {{ padding: 5px 9px; }}
  .enc-items tfoot td {{ border-top: 1px solid var(--rule); background: var(--bg-rec); }}
  @media (max-width: 480px) {{
    .party-card {{ flex-direction: column; }}
    .party-portrait {{ width: 100%; height: auto; max-width: 220px; }}
  }}

  /* ---- Misc primitives ---- */
  .clearfix::after {{ content: ''; display: table; clear: both; }}
  .muted {{ color: var(--ink-muted); font-size: 0.92em; }}
  pre {{
    background: var(--bg-deep);
    padding: 14px 16px;
    border-radius: 3px;
    border: 1px solid var(--rule);
    overflow-x: auto;
    font-size: 0.86em;
    font-family: var(--font-mono);
  }}
  code {{ font-family: var(--font-mono); font-size: 0.92em; color: var(--accent-gold-hi); }}
  pre code {{ color: var(--ink-body); }}
  blockquote {{
    border-left: 3px solid var(--accent-gold);
    padding: 4px 0 4px 16px;
    color: #b8a982;
    font-style: italic;
    margin: 12px 0;
  }}
  hr {{
    border: none;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--rule-hi), transparent);
    margin: 28px 0;
  }}
  table {{ width: 100%; border-collapse: collapse; margin: 14px 0; }}
  th, td {{ border: 1px solid var(--rule); padding: 7px 11px; text-align: left; }}
  th {{
    background: linear-gradient(to bottom, #2c2218, #221912);
    color: var(--accent-gold);
    font-family: var(--font-display);
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 0.84em;
  }}
  img:not(#lb-img) {{ cursor: zoom-in; }}

  /* ---- Spoiler accordion (rumors, DM notes, sheet descriptions) ---- */
  details.spoiler {{
    border: 1px solid var(--rule);
    border-left: 3px solid var(--accent-rust);
    background: linear-gradient(to bottom, #1d1410, #16100b);
    border-radius: 3px;
    margin: 12px 0;
    box-shadow: var(--inset-warm);
  }}
  details.spoiler[open] {{ border-left-color: var(--accent-gold); }}
  details.spoiler > summary {{
    cursor: pointer;
    padding: 11px 16px;
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.78em;
    color: var(--accent-rust);
    user-select: none;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 12px;
    transition: color 180ms ease;
  }}
  details.spoiler > summary::-webkit-details-marker {{ display: none; }}
  details.spoiler > summary::before {{
    content: '▶';
    font-size: 0.7em;
    transition: transform 200ms ease;
    color: inherit;
  }}
  details.spoiler[open] > summary {{
    color: var(--accent-gold);
    border-bottom: 1px solid var(--rule);
  }}
  details.spoiler[open] > summary::before {{ transform: rotate(90deg); }}
  details.spoiler > summary .spoiler-warn {{
    color: var(--ink-muted);
    font-style: italic;
    font-family: var(--font-body);
    text-transform: none;
    letter-spacing: 0.01em;
    font-size: 0.86em;
    margin-left: auto;
    font-weight: normal;
  }}
  details.spoiler > .spoiler-body {{
    padding: 14px 18px;
  }}
  details.spoiler > .spoiler-body > *:first-child {{ margin-top: 0; }}
  details.spoiler > .spoiler-body > *:last-child {{ margin-bottom: 0; }}

  /* Compact details (e.g. portrait description on character sheets) — quieter than .spoiler. */
  details.portrait-desc {{ margin-top: 8px; text-align: left; }}
  details.portrait-desc > summary {{
    cursor: pointer;
    font-size: 0.78em;
    color: var(--ink-muted);
    font-style: italic;
    list-style: none;
    user-select: none;
    padding: 2px 0;
  }}
  details.portrait-desc > summary::-webkit-details-marker {{ display: none; }}
  details.portrait-desc > summary::before {{
    content: '▸ ';
    color: var(--accent-gold);
    font-style: normal;
  }}
  details.portrait-desc[open] > summary::before {{ content: '▾ '; }}
  details.portrait-desc > p {{
    margin-top: 6px;
    color: var(--ink-muted);
    font-size: 0.84em;
    line-height: 1.5;
    text-align: left;
  }}

  /* ---- Lightbox ---- */
  #lb {{
    display: none; position: fixed; inset: 0;
    background: rgba(0,0,0,.92); z-index: 9999;
    align-items: center; justify-content: center;
    padding: 20px; cursor: zoom-out;
    backdrop-filter: blur(4px);
  }}
  #lb.open {{ display: flex; }}
  #lb img {{
    max-width: 90vw; max-height: 90vh; object-fit: contain;
    border-radius: 3px; border: 1px solid var(--accent-gold);
    box-shadow: 0 12px 60px rgba(0,0,0,.95); cursor: zoom-out;
  }}
</style>
</head>
<body>
<nav>
  <a href="/">Gallery</a>
  <a href="/play">Play</a>
  <a href="/party">Party</a>
  <a href="/world">World</a>
  <a href="/quests">Quests</a>
  <a href="/log">Chronicle</a>
  <a href="/sessions">Sessions</a>
  <a href="/characters">Characters</a>
  <a href="/locations">Locations</a>
  <a href="/monsters">Monsters</a>
  <a href="/spells">Spells</a>
  <a href="/classes">Classes</a>
  <a href="/items">Items</a>
  <a href="/combat">Combat</a>
  <a href="/maps">Maps</a>
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
  // Highlight the active nav link based on the current path.
  var p = location.pathname;
  document.querySelectorAll('nav a').forEach(function(a){{
    var href = a.getAttribute('href');
    if (href === '/' ? p === '/' : p.indexOf(href) === 0) a.classList.add('is-active');
  }});
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
    combat_hp = _combat_hp_overrides()
    combat_effects = _combat_effects_overrides()
    image_index = _load_image_index()
    light_sources = state.get("light_sources", []) or []

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
        hp_cur = combat_hp.get(key, cstate.get("hp", hp_max))
        label = char.get("label", key)
        cls = char.get("cls", "")
        level = int(char.get("level", 1) or 1)
        ac = char.get("ac")
        thac0 = char.get("thac0")
        xp = cstate.get("xp", 0)
        conditions = cstate.get("conditions", [])

        bar = _hp_bar(hp_cur, hp_max)
        cond_tags = "".join(f'<span class="tag">{html.escape(c)}</span>' for c in conditions)

        # Portrait
        portrait = _find_portrait(label, image_index, key)
        portrait_html = ""
        if portrait:
            fn = html.escape(portrait["filename"])
            portrait_html = (
                f'<img src="/images/{fn}" alt="{html.escape(label)}" '
                f'class="party-portrait">'
            )

        # Class line: "Cleric 3"
        cls_line = html.escape(cls)
        if cls and level:
            cls_line = f"{html.escape(cls)} {level}"

        # Combat stats line
        stats_parts = []
        if ac is not None:
            stats_parts.append(f"<strong>AC:</strong> {html.escape(str(ac))}")
        if thac0 is not None:
            stats_parts.append(f"<strong>THAC0:</strong> {html.escape(str(thac0))}")
        stats_html = ""
        if stats_parts:
            stats_html = "<p>" + " &nbsp;·&nbsp; ".join(stats_parts) + "</p>"

        # XP / next-level threshold
        xp_table = _class_xp_table(cls)
        next_xp_str = ""
        if xp_table and level < len(xp_table):
            next_xp = xp_table[level]   # xp_required for (level+1) — table is 0-indexed by level
            if next_xp > xp:
                next_xp_str = f' <span class="muted">/ {next_xp:,}</span>'
            else:
                next_xp_str = ' <span class="tag" style="background:#1a3a18;color:#6db86d">level up</span>'

        # Active effects (combat-only)
        effects = combat_effects.get(key, [])
        effects_html = ""
        if effects:
            chips = []
            for eff in effects:
                name = html.escape(eff.get("name", "?"))
                dur = eff.get("duration", 0)
                mods = []
                for k, sym in (("to_hit", "hit"), ("ac", "AC"), ("dmg", "dmg"), ("save", "save")):
                    v = eff.get(k)
                    if v:
                        mods.append(f"{sym}{int(v):+d}")
                mod_str = f" <span class='muted'>({', '.join(mods)})</span>" if mods else ""
                chips.append(
                    f'<span class="effect-chip">{name} '
                    f'<span class="muted">{dur}r</span>{mod_str}</span>'
                )
            effects_html = "<p><strong>Effects:</strong> " + " ".join(chips) + "</p>"

        # Encumbrance — only show when not "light" (the common case)
        enc = _encumbrance_band(char, state, key)
        enc_html = ""
        if enc["band"] != "light":
            color = {
                "moderate":   "#c8a96e",
                "heavy":      "#e0a060",
                "severe":     "#e08060",
                "overloaded": "#ff6a4a",
            }.get(enc["band"], "#c8a96e")
            penalty_str = f" ({enc['penalty']:+d} move)" if enc["penalty"] else ""
            enc_html = (
                f'<p><strong>Load:</strong> '
                f'<span style="color:{color}">{html.escape(enc["band"])}</span> '
                f'<span class="muted">— {enc["weight"]} lb{penalty_str}</span></p>'
            )

        # Light source carried by this character
        carried_lit = [s for s in light_sources
                       if s.get("lit") and s.get("holder") == key]
        light_chip_html = ""
        if carried_lit:
            chips = []
            for s in carried_lit:
                mins = s.get("minutes_remaining", 0)
                cls_chip = "light-chip guttering" if mins <= 5 else "light-chip"
                stype = html.escape(s.get("type", "?"))
                chips.append(f'<span class="{cls_chip}">{stype} · {mins} min</span>')
            light_chip_html = "<p><strong>Carrying:</strong> " + " ".join(chips) + "</p>"

        # Spell slots
        slot_cfg = char.get("spell_slots") or {}
        slot_state = cstate.get("spell_slots") or {}
        slot_lines = []
        for lvl, max_count in slot_cfg.items():
            remaining = slot_state.get(str(lvl), max_count)
            slot_lines.append(f"L{lvl}: {remaining}/{max_count}")
        slots_html = ""
        if slot_lines:
            slots_html = "<p><strong>Slots:</strong> " + " &nbsp; ".join(slot_lines) + "</p>"

        # Memorized spells, itemized and grouped by spell level.
        memorized = cstate.get("memorized_spells") or []
        mem_html = ""
        if memorized:
            # Resolve missing levels from the spell DB (entries are often
            # stored with level=0 by the MCP layer).
            unknown_names = sorted({
                (s.get("name") if isinstance(s, dict) else str(s)).strip()
                for s in memorized
                if not (isinstance(s, dict) and int(s.get("level", 0) or 0) > 0)
            })
            unknown_names = [n for n in unknown_names if n]
            level_map: dict[str, int] = {}
            if unknown_names:
                placeholders = ",".join("?" * len(unknown_names))
                rows = _db(_2E_DB).execute(
                    f"SELECT name, level FROM spells WHERE lower(name) IN ({placeholders})",
                    [n.lower() for n in unknown_names],
                ).fetchall()
                level_map = {r["name"].lower(): int(r["level"]) for r in rows}

            by_level: dict[int, list[dict]] = {}
            for s in memorized:
                if isinstance(s, dict):
                    name = (s.get("name") or "").strip()
                    lvl = int(s.get("level", 0) or 0)
                    cast = bool(s.get("cast"))
                else:
                    name, lvl, cast = str(s).strip(), 0, False
                if lvl <= 0:
                    lvl = level_map.get(name.lower(), 0)
                by_level.setdefault(lvl, []).append({"name": name, "cast": cast})

            def _render_spell_li(s: dict) -> str:
                name_esc = html.escape(s["name"])
                if s["cast"]:
                    return (
                        f'<li class="spell-cast" title="cast">'
                        f'<s>{name_esc}</s></li>'
                    )
                return f'<li class="spell-ready">{name_esc}</li>'

            blocks = []
            for lvl in sorted(by_level):
                spells_at_level = by_level[lvl]
                ready_at_level = sum(1 for s in spells_at_level if not s["cast"])
                total_at_level = len(spells_at_level)
                label_lvl = f"Level {lvl}" if lvl > 0 else "Unleveled"
                items = "".join(_render_spell_li(s) for s in spells_at_level)
                blocks.append(
                    f'<div class="spell-level">'
                    f'<div class="spell-level-head">{label_lvl} '
                    f'<span class="muted">({ready_at_level}/{total_at_level})</span></div>'
                    f'<ul class="spell-list">{items}</ul>'
                    f'</div>'
                )

            ready_count = sum(1 for s in memorized
                              if not (isinstance(s, dict) and s.get("cast")))
            total_count = len(memorized)
            mem_html = (
                f'<div class="memorized">'
                f'<p style="margin-bottom:4px"><strong>Memorized</strong> '
                f'<span class="muted">({ready_count}/{total_count} ready)</span></p>'
                f'{"".join(blocks)}'
                f'</div>'
            )

        cards.append(
            f'<div class="card party-card clearfix">'
            f'{portrait_html}'
            f'<div class="party-body">'
            f'<h2><a href="/sheets/{html.escape(key)}">{html.escape(label)}</a></h2>'
            f"<p class='muted'>{cls_line}</p>"
            f"<p><strong>HP:</strong> {html.escape(str(hp_cur))}/{html.escape(str(hp_max))} {bar}</p>"
            f"{stats_html}"
            f"<p><strong>XP:</strong> {xp:,}{next_xp_str}</p>"
            f"{enc_html}"
            f"{light_chip_html}"
            f"{effects_html}"
            f"{slots_html}"
            f"{mem_html}"
            f'<p>{cond_tags}</p>'
            f'</div>'
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

    rumor_count = len(rumors)
    secret_count = len(secret_rows)

    body = (
        f"<h1>{html.escape(campaign_name)} — World</h1>"
        + _world_info_bar(state)
        + "<h2>Factions</h2>" + factions_html
        + "<h2>World Clocks</h2>" + clocks_html
        + '<details class="spoiler">'
        + f'<summary>Rumors <span class="muted">({rumor_count})</span>'
        + '<span class="spoiler-warn">DM view — reveals truth tier · click to expand</span>'
        + '</summary>'
        + f'<div class="spoiler-body">{rumors_html}</div>'
        + '</details>'
        + '<details class="spoiler">'
        + f'<summary>DM-Only Notes <span class="muted">({secret_count})</span>'
        + '<span class="spoiler-warn">Never narrate to the player · click to expand</span>'
        + '</summary>'
        + f'<div class="spoiler-body">{secrets_html}</div>'
        + '</details>'
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


def _sessions_dir() -> Path:
    return _export.PROJECTS_DIR / _export.encode_cwd(Path(__file__).parent)


def _session_teaser(path: Path, max_chars: int = 140) -> str:
    """First non-empty player prompt in the session, trimmed."""
    try:
        with path.open() as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "user":
                    continue
                content = rec.get("message", {}).get("content", "")
                if not isinstance(content, str):
                    continue
                cleaned = _export.clean_user_text(content)
                if cleaned:
                    cleaned = cleaned.replace("\n", " ").strip()
                    return cleaned[:max_chars] + ("…" if len(cleaned) > max_chars else "")
    except OSError:
        pass
    return ""


@app.route("/sessions")
def sessions_index():
    sdir = _sessions_dir()
    if not sdir.is_dir():
        body = "<h1>Sessions</h1><p class='muted'>No transcript directory found.</p>"
        return render("Sessions", body)

    files = _export.list_sessions(sdir)
    rows = []
    for p in files:
        st = p.stat()
        when = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        size_kb = st.st_size / 1024
        teaser = html.escape(_session_teaser(p))
        sid = p.stem
        rows.append(
            f'<tr>'
            f'<td><a href="/sessions/{html.escape(sid)}"><code>{html.escape(sid[:8])}</code></a></td>'
            f'<td class="muted">{when}</td>'
            f'<td class="muted" style="text-align:right">{size_kb:,.1f} KB</td>'
            f'<td>{teaser or "<span class=muted>—</span>"}</td>'
            f'</tr>'
        )

    if not rows:
        body = "<h1>Sessions</h1><p class='muted'>No sessions yet.</p>"
    else:
        body = (
            "<h1>Sessions</h1>"
            f"<p class='muted'>{len(rows)} session(s). Newest first.</p>"
            '<div class="card"><table style="width:100%;border-collapse:collapse">'
            "<thead><tr>"
            '<th style="text-align:left;padding:6px 8px">ID</th>'
            '<th style="text-align:left;padding:6px 8px">When</th>'
            '<th style="text-align:right;padding:6px 8px">Size</th>'
            '<th style="text-align:left;padding:6px 8px">First prompt</th>'
            "</tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
    return render("Sessions", body)


@app.route("/sessions/<sid>")
def session_view(sid: str):
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", sid):
        abort(404)
    sdir = _sessions_dir()
    path = sdir / f"{sid}.jsonl"
    if not path.is_file():
        abort(404)

    include_tools = request.args.get("tools", "0") not in ("0", "", "false")
    dm_only = request.args.get("dm_only", "0") not in ("0", "", "false")

    turns = _export.parse_session(path)
    md = _export.render(
        turns,
        include_tools=include_tools,
        dm_only=dm_only,
        player_only=False,
        show_timestamps=True,
    )
    content_html = _markdown_to_html(md)

    when = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    toggle = (
        f'<a href="/sessions/{html.escape(sid)}?tools={"0" if include_tools else "1"}'
        f'{"&dm_only=1" if dm_only else ""}">'
        f'{"Hide" if include_tools else "Show"} tool calls</a> · '
        f'<a href="/sessions/{html.escape(sid)}?dm_only={"0" if dm_only else "1"}'
        f'{"&tools=1" if include_tools else ""}">'
        f'{"Show all turns" if dm_only else "DM only"}</a>'
    )

    body = (
        f"<h1>Session <code>{html.escape(sid[:8])}</code></h1>"
        f"<p class='muted'>{when} · {toggle} · "
        f'<a href="/sessions">← back to index</a></p>'
        f'<div class="card">{content_html}</div>'
    )
    return render(f"Session {sid[:8]}", body)


# ---------------------------------------------------------------------------
# Live play surface — drives `claude -p` via tools/dm_session.py
# ---------------------------------------------------------------------------

def _latest_location_name() -> str:
    """Most recent location_visited from events.json, or empty string."""
    if not cfg:
        return ""
    p = cfg["_dir"] / "events.json"
    if not p.exists():
        return ""
    try:
        events = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ""
    for ev in reversed(events):
        if ev.get("type") == "location_visited":
            return ev.get("name") or ev.get("slug") or ""
    return ""


def _play_state() -> dict:
    """Assemble the live data the /play surface needs: party (with portraits
    and HP), plus the equivalent of the statusline footer (location, time,
    weather, day, session, combat, coin)."""
    if not cfg:
        return {"campaign": None, "characters": [], "session": 0, "day": 0,
                "time": "", "weather": "", "location": "", "in_combat": False, "coin": ""}

    _reload_cfg()
    state = _c.load_state(cfg)
    combat_hp = _combat_hp_overrides()
    image_index = _load_image_index()

    chars = []
    for key, char in cfg.get("characters", {}).items():
        cstate = state.get("characters", {}).get(key, {})
        hp_max = char.get("hp_max", 0)
        hp_cur = combat_hp.get(key, cstate.get("hp", hp_max))
        portrait = _find_portrait(char.get("label", key), image_index, key)
        chars.append({
            "key": key,
            "label": char.get("label", key),
            "cls": char.get("cls", ""),
            "level": int(char.get("level", 1) or 1),
            "hp": hp_cur,
            "hp_max": hp_max,
            "ac": char.get("ac"),
            "thac0": char.get("thac0"),
            "conditions": cstate.get("conditions", []),
            "portrait": f"/images/{portrait['filename']}" if portrait else None,
        })

    coin = state.get("coin", {}) or {}
    coin_parts = [f"{coin[d]} {d}" for d in ("gp", "ep", "sp", "cp") if coin.get(d)]

    hour = state.get("current_hour")
    minute = state.get("current_minute")
    time_str = f"{hour:02d}:{minute:02d}" if isinstance(hour, int) and isinstance(minute, int) else ""
    weather_full = state.get("current_weather") or ""
    weather_label = weather_full.split(" (", 1)[0] if weather_full else ""

    return {
        "campaign": {"name": cfg.get("name"), "slug": cfg.get("_name")},
        "session": state.get("current_session", 0),
        "day": state.get("current_day", 1),
        "time": time_str,
        "weather": weather_label,
        "location": _latest_location_name(),
        "in_combat": bool(state.get("combat")),
        "coin": " · ".join(coin_parts),
        "characters": chars,
    }


_PLAY_HTML = """
<style>
  /* Take the entire viewport on /play — narrative window grows to fill all
     remaining width to the left of the sidebar. The :has() check scopes this
     to the play page so other routes keep the standard 980px container. */
  .container:has(.play-shell) {
    max-width: none;
    width: 100%;
    padding-left: 28px;
    padding-right: 28px;
  }
  .play-shell {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 320px;
    gap: 18px;
    height: calc(100vh - 110px);
  }
  .play-main { display: flex; flex-direction: column; min-width: 0; }
  .play-side { min-width: 0; overflow-y: auto; }

  .play-toolbar {
    display: flex;
    align-items: baseline;
    gap: 14px;
    margin-bottom: 14px;
  }
  .play-toolbar h1 {
    font-size: 1.45em;
    border-bottom: none;
    padding: 0;
    margin: 0;
    flex: 0 0 auto;
    color: var(--ink-display);
    letter-spacing: 0.18em;
  }
  .play-toolbar .sid {
    color: var(--ink-muted);
    font-size: 0.82em;
    font-family: var(--font-display);
    letter-spacing: 0.14em;
    text-transform: uppercase;
  }
  .play-toolbar .sid code {
    color: var(--accent-gold);
    font-family: var(--font-mono);
    text-transform: none;
    letter-spacing: 0;
    margin-left: 6px;
  }
  .play-toolbar .spacer { flex: 1; }
  .play-toolbar button {
    background: transparent;
    color: var(--accent-gold);
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 5px 12px;
    font-family: var(--font-display);
    font-size: 0.7em;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    cursor: pointer;
    transition: all 180ms ease;
  }
  .play-toolbar button:hover {
    border-color: var(--accent-gold);
    color: var(--accent-gold-hi);
  }
  .play-toolbar #play-status {
    color: var(--ink-muted);
    font-size: 0.82em;
    font-style: italic;
    min-width: 80px;
  }

  /* Filter strip — toggles which message types are visible. */
  .play-filters {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 0 0 10px;
  }
  .play-filters .filter-label {
    color: var(--ink-muted);
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-size: 0.66em;
    margin-right: 4px;
  }
  .turn-toggle {
    background: transparent;
    border: 1px solid var(--rule);
    border-radius: 2px;
    padding: 3px 11px;
    font-family: var(--font-display);
    font-size: 0.68em;
    text-transform: uppercase;
    letter-spacing: 0.18em;
    cursor: pointer;
    color: var(--ink-muted);
    transition: all 180ms ease;
  }
  .turn-toggle::before { content: '○ '; opacity: 0.55; }
  .turn-toggle.active {
    border-color: var(--accent-gold);
    color: var(--accent-gold-hi);
    background: rgba(200,169,110,0.08);
  }
  .turn-toggle.active::before { content: '● '; opacity: 1; color: var(--accent-gold); }
  .turn-toggle:hover { border-color: var(--rule-hi); }
  .turn-toggle.active:hover { border-color: var(--accent-gold-hi); }

  /* Visibility filters applied to .play-log via JS (toggle pills + localStorage). */
  .play-log.hide-tools .turn.tool { display: none; }
  .play-log.hide-meta  .turn.meta { display: none; }
  .play-log.hide-player .turn.player { display: none; }

  .play-log {
    flex: 1;
    overflow-y: auto;
    background:
      linear-gradient(135deg, rgba(255,220,180,0.022), transparent 50%),
      linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 24px 30px;
    box-shadow: var(--inset-warm), var(--shadow-card);
    line-height: 1.72;
    font-size: 1.02em;
  }
  .play-log .empty-state {
    text-align: center;
    color: var(--ink-muted);
    font-style: italic;
    margin: 24px auto 0;
    max-width: 520px;
  }
  .play-log .empty-state .play-hero {
    display: block;
    width: min(360px, 70%);
    margin: 0 auto 20px;
    border-radius: 4px;
    border: 1px solid var(--rule-hi);
    box-shadow: 0 8px 28px rgba(0,0,0,0.55), inset 0 0 0 1px rgba(0,0,0,0.4);
    cursor: default;
    opacity: 0.96;
  }
  .play-log .empty-state .empty-state-text {
    font-size: 0.95em;
    letter-spacing: 0.02em;
    color: var(--ink-muted);
  }
  .play-log .empty-state .empty-state-text::before {
    content: '✦';
    color: var(--accent-gold);
    font-style: normal;
    margin-right: 8px;
    font-size: 0.9em;
  }
  .turn { margin-bottom: 18px; }
  .turn.player {
    border-left: 2px solid var(--accent-gold);
    padding: 2px 0 2px 16px;
    color: #c8b48a;
    font-style: italic;
    white-space: pre-wrap;
  }
  .turn.player::before {
    content: '◇';
    color: var(--accent-gold);
    margin-right: 8px;
    font-style: normal;
    font-size: 0.85em;
  }
  .turn.dm {
    color: var(--ink-body);
    white-space: pre-wrap;
  }
  .turn.tool {
    font-family: var(--font-mono);
    font-size: 0.78em;
    color: var(--ink-muted);
    background: rgba(15,11,7,0.55);
    border-left: 1px solid var(--rule);
    padding: 5px 12px;
    margin: 8px 0;
    border-radius: 0 2px 2px 0;
  }
  .turn.tool::before {
    content: '⚙ ';
    color: var(--accent-gold);
    margin-right: 4px;
  }
  .turn.error {
    color: #e08070;
    border-left: 2px solid var(--accent-blood);
    padding: 6px 12px;
    background: rgba(70,20,15,0.3);
  }
  .turn.meta {
    color: var(--ink-muted);
    font-size: 0.78em;
    text-align: center;
    margin: 18px 0 6px;
    font-family: var(--font-display);
    letter-spacing: 0.18em;
    text-transform: uppercase;
    position: relative;
  }
  .turn.meta::before, .turn.meta::after {
    content: '';
    position: absolute;
    top: 50%;
    width: 25%;
    height: 1px;
    background: linear-gradient(to right, transparent, var(--rule-hi), transparent);
  }
  .turn.meta::before { left: 8%; }
  .turn.meta::after { right: 8%; transform: scaleX(-1); }

  .play-form {
    margin-top: 14px;
    display: grid;
    grid-template-columns: 1fr auto;
    gap: 10px;
  }
  .play-form textarea {
    background: var(--bg-rec);
    color: var(--ink-body);
    border: 1px solid var(--rule);
    border-radius: 3px;
    padding: 14px 16px;
    font-family: var(--font-body);
    font-size: 1.14em;
    resize: vertical;
    min-height: 68px;
    line-height: 1.55;
    transition: border-color 200ms ease, box-shadow 200ms ease;
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.3);
  }
  .play-form textarea:focus {
    outline: none;
    border-color: var(--accent-gold);
    box-shadow: inset 0 1px 3px rgba(0,0,0,0.3), 0 0 0 1px rgba(200,169,110,0.25);
  }
  .play-form textarea::placeholder { color: var(--ink-muted); font-style: italic; }
  .play-form button {
    background: linear-gradient(to bottom, #4a3625, #2e2014);
    color: var(--ink-display);
    border: 1px solid var(--rule-hi);
    border-radius: 3px;
    width: 64px;
    padding: 0;
    cursor: pointer;
    box-shadow: var(--inset-warm), var(--shadow-card);
    transition: all 180ms ease;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .play-form button svg {
    width: 26px;
    height: 26px;
    transition: transform 180ms ease;
  }
  .play-form button:hover:not(:disabled) {
    border-color: var(--accent-gold);
    color: var(--accent-gold-hi);
    background: linear-gradient(to bottom, #5a4030, #3a2818);
  }
  .play-form button:hover:not(:disabled) svg { transform: translateX(2px); }
  .play-form button:disabled { opacity: 0.4; cursor: not-allowed; }

  /* Generated scene image inline in the chat log. */
  .turn.scene {
    text-align: center;
    margin: 22px 0;
  }
  .turn.scene img {
    max-width: 100%;
    max-height: 460px;
    border-radius: 4px;
    border: 1px solid var(--rule-hi);
    box-shadow: var(--shadow-deep);
    display: inline-block;
  }
  .turn.scene .caption {
    display: block;
    font-family: var(--font-display);
    font-size: 0.7em;
    color: var(--ink-muted);
    text-transform: uppercase;
    letter-spacing: 0.2em;
    margin-top: 8px;
    font-style: normal;
  }
  .turn.generating {
    text-align: center;
    color: var(--ink-muted);
    font-style: italic;
    font-size: 0.88em;
    padding: 14px;
    border: 1px dashed var(--rule);
    border-radius: 3px;
    margin: 18px 0;
  }
  .turn.generating::before {
    content: '◌';
    color: var(--accent-gold);
    font-style: normal;
    margin-right: 8px;
    display: inline-block;
    animation: spin 1.6s linear infinite;
  }
  @keyframes spin {
    from { transform: rotate(0deg); }
    to { transform: rotate(360deg); }
  }

  /* ---- Sidebar ---- */
  .play-side {
    background: linear-gradient(to bottom, var(--bg-card-hi), var(--bg-card));
    border: 1px solid var(--rule);
    border-radius: 4px;
    padding: 18px 16px 14px;
    box-shadow: var(--inset-warm), var(--shadow-card);
  }

  /* Campaign banner at the very top of the sidebar. */
  .side-camp {
    font-family: var(--font-display);
    color: var(--accent-gold-hi);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    text-align: center;
    font-size: 0.82em;
    font-weight: 500;
    line-height: 1.35;
    padding-bottom: 12px;
    margin-bottom: 12px;
    border-bottom: 1px solid var(--rule);
  }
  .side-camp:empty { display: none; }

  /* Stats rows: session/day/time, weather/gold, location, combat. */
  .side-stats { display: flex; flex-direction: column; gap: 9px; margin-bottom: 12px; }
  .side-row {
    display: flex;
    align-items: baseline;
    justify-content: center;
    gap: 16px;
    flex-wrap: wrap;
  }
  .side-row .pip { display: inline-flex; align-items: baseline; gap: 6px; line-height: 1; }
  .side-row .pip .pip-label {
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.18em;
    font-size: 0.62em;
    color: var(--ink-muted);
  }
  .side-row .pip .pip-val {
    font-family: var(--font-body);
    color: var(--ink-body);
    font-size: 0.96em;
  }
  .side-row.row-loc { padding-top: 4px; }
  .side-row.row-loc .pip-val {
    color: var(--accent-gold-hi);
    font-style: italic;
    font-size: 1.0em;
  }
  .side-row.row-combat {
    padding: 4px 10px;
    background: rgba(70,18,15,0.4);
    border: 1px solid #6a2818;
    border-radius: 2px;
  }
  .side-row.row-combat .pip-val {
    color: var(--accent-blood);
    font-family: var(--font-display);
    text-transform: uppercase;
    letter-spacing: 0.22em;
    font-size: 0.8em;
    font-weight: 500;
    animation: combatPulse 1.6s ease-in-out infinite;
  }
  @keyframes combatPulse {
    0%, 100% {{ opacity: 1; }}
    50% {{ opacity: 0.55; }}
  }
  .pip.pip-gold .pip-val {
    color: var(--accent-gold);
    font-family: var(--font-mono);
    font-size: 0.88em;
    letter-spacing: 0;
  }

  .side-title {
    font-family: var(--font-display);
    color: var(--accent-gold);
    text-transform: uppercase;
    letter-spacing: 0.26em;
    font-size: 0.74em;
    text-align: center;
    margin: 0 0 14px;
    padding: 10px 0;
    border-top: 1px solid var(--rule);
    border-bottom: 1px solid var(--rule);
    position: relative;
  }
  .side-title::after {
    content: '◆';
    position: absolute;
    left: 50%;
    bottom: -7px;
    transform: translateX(-50%);
    background: var(--bg-card-hi);
    color: var(--accent-gold);
    padding: 0 6px;
    font-size: 0.6em;
    letter-spacing: 0;
  }
  .party-list { display: flex; flex-direction: column; gap: 16px; }
  .party-mini { display: flex; gap: 12px; align-items: flex-start; }
  .portrait-frame {
    flex-shrink: 0;
    width: 58px; height: 58px;
    border-radius: 50%;
    background: var(--bg-deep);
    border: 1px solid var(--accent-gold);
    box-shadow: 0 0 0 1px var(--bg-deep), 0 0 0 2px var(--rule), 0 2px 6px rgba(0,0,0,0.5);
    overflow: hidden;
    position: relative;
    text-decoration: none;
    color: inherit;
    display: block;
    transition: box-shadow 200ms ease, transform 200ms ease;
  }
  a.portrait-frame:hover {
    box-shadow: 0 0 0 1px var(--bg-deep), 0 0 0 2px var(--accent-gold-hi), 0 4px 12px rgba(200,169,110,0.25);
    transform: translateY(-1px);
  }
  .portrait-frame img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .portrait-frame.empty {
    background: linear-gradient(135deg, #1a130c, #0f0a07);
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--rule-hi);
    font-family: var(--font-display);
    font-size: 1.4em;
    letter-spacing: 0;
  }
  .portrait-frame.kod {
    border-color: var(--hp-bad);
    box-shadow: 0 0 0 1px var(--bg-deep), 0 0 0 2px #4a1a14, 0 0 12px rgba(200,80,58,0.4);
    filter: grayscale(0.4);
  }

  .party-mini-body { flex: 1; min-width: 0; }
  .party-mini-name {
    font-family: var(--font-display);
    color: var(--ink-display);
    font-size: 0.94em;
    letter-spacing: 0.06em;
    line-height: 1.2;
    display: block;
    text-decoration: none;
    transition: color 180ms ease;
  }
  a.party-mini-name:hover {
    color: var(--accent-gold-hi);
    text-decoration: underline;
    text-decoration-color: var(--rule-hi);
    text-underline-offset: 3px;
  }
  .party-mini-class {
    font-family: var(--font-body);
    font-style: italic;
    color: var(--ink-muted);
    font-size: 0.84em;
    margin-top: 1px;
  }
  .party-mini-hp {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 6px;
  }
  .party-mini-hp .hp-meter { flex: 1; }
  .party-mini-hp-text {
    font-family: var(--font-mono);
    font-size: 0.74em;
    color: var(--ink-muted);
    min-width: 42px;
    text-align: right;
    letter-spacing: 0;
  }
  .party-mini-conds {
    margin-top: 5px;
    display: flex;
    flex-wrap: wrap;
    gap: 3px;
  }
  .cond-chip {
    font-family: var(--font-display);
    font-size: 0.62em;
    padding: 1px 6px;
    border: 1px solid var(--accent-rust);
    background: #2e1d12;
    color: #e8b487;
    text-transform: uppercase;
    letter-spacing: 0.14em;
    border-radius: 1px;
  }

  @media (max-width: 900px) {
    .play-shell {
      grid-template-columns: 1fr;
      height: auto;
    }
    .play-side { max-height: 480px; }
  }
</style>

<div class="play-shell">
  <section class="play-main">
    <div class="play-toolbar">
      <h1>Live Play</h1>
      <span class="sid">session<code id="play-sid">…</code></span>
      <span class="spacer"></span>
      <span id="play-status"></span>
      <button id="play-make-pic" type="button" title="Generate an illustration of the last DM reply">Make picture</button>
      <button id="play-reset" type="button">New session</button>
    </div>
    <div class="play-filters">
      <span class="filter-label">Show:</span>
      <button class="turn-toggle" data-filter="player" type="button">Player echoes</button>
      <button class="turn-toggle" data-filter="tools" type="button">Tool calls</button>
      <button class="turn-toggle" data-filter="meta" type="button">Meta</button>
    </div>
    <div class="play-log" id="play-log">
      <div class="empty-state">
        <img src="/static/play-hero.png" alt="" class="play-hero">
        <div class="empty-state-text">Begin when ready. Describe an action below to address the DM.</div>
      </div>
    </div>
    <form class="play-form" id="play-form">
      <textarea id="play-input" placeholder="Describe your action…  (Ctrl+Enter to send)" autocomplete="off"></textarea>
      <button type="submit" id="play-send" title="Send (Ctrl+Enter)" aria-label="Send">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3.5 11.6 21 4l-7.5 17-2.2-7.4-7.8-2z"/>
          <path d="m11.3 13.6 4.6-4.6" opacity="0.7"/>
        </svg>
      </button>
    </form>
  </section>

  <aside class="play-side">
    <div class="side-camp" id="side-camp"></div>
    <div class="side-stats" id="side-stats"></div>
    <div class="side-title">The Party</div>
    <div class="party-list" id="party-list">
      <div class="muted" style="text-align:center; font-size:0.84em; font-style:italic">…</div>
    </div>
  </aside>
</div>

<script>
(function(){
  const log = document.getElementById('play-log');
  const input = document.getElementById('play-input');
  const send = document.getElementById('play-send');
  const form = document.getElementById('play-form');
  const sidEl = document.getElementById('play-sid');
  const statusEl = document.getElementById('play-status');
  const resetBtn = document.getElementById('play-reset');
  const partyList = document.getElementById('party-list');
  const sideCamp = document.getElementById('side-camp');
  const sideStats = document.getElementById('side-stats');
  const makePicBtn = document.getElementById('play-make-pic');
  let firstAppend = true;
  let lastDmText = '';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
      {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }

  function append(role, text) {
    if (firstAppend) {
      log.innerHTML = '';
      firstAppend = false;
    }
    const div = document.createElement('div');
    div.className = 'turn ' + role;
    div.textContent = text;
    log.appendChild(div);
    log.scrollTop = log.scrollHeight;
  }

  function handleEvent(evt) {
    if (evt.type === 'system' && evt.session_id) {
      sidEl.textContent = evt.session_id.slice(0, 8);
    } else if (evt.type === 'assistant' && evt.message && Array.isArray(evt.message.content)) {
      for (const block of evt.message.content) {
        if (block.type === 'text' && block.text) {
          append('dm', block.text);
          lastDmText = block.text;  // remember for "Make picture"
        } else if (block.type === 'tool_use') {
          const args = block.input ? Object.keys(block.input).slice(0, 3).join(', ') : '';
          append('tool', (block.name || '?') + '(' + args + ')');
        }
      }
    } else if (evt.type === 'result') {
      const parts = ['turn complete'];
      if (typeof evt.duration_ms === 'number') parts.push((evt.duration_ms / 1000).toFixed(1) + 's');
      if (typeof evt.total_cost_usd === 'number') parts.push('$' + evt.total_cost_usd.toFixed(4));
      append('meta', parts.join(' · '));
      refreshState();
    } else if (evt.type === 'error') {
      append('error', 'rc=' + evt.returncode + '  ' + (evt.stderr || '').slice(0, 500));
    }
  }

  async function submitTurn(text) {
    append('player', text);
    send.disabled = true;
    statusEl.textContent = 'thinking…';
    try {
      const resp = await fetch('/play/turn', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({message: text}),
      });
      if (!resp.ok) {
        append('error', 'HTTP ' + resp.status);
        return;
      }
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buf = '';
      while (true) {
        const {value, done} = await reader.read();
        if (done) break;
        buf += decoder.decode(value, {stream: true});
        let nl;
        while ((nl = buf.indexOf('\\n')) >= 0) {
          const line = buf.slice(0, nl).trim();
          buf = buf.slice(nl + 1);
          if (!line) continue;
          try { handleEvent(JSON.parse(line)); }
          catch (e) { append('error', 'bad json: ' + line.slice(0, 80)); }
        }
      }
    } catch (err) {
      append('error', String(err));
    } finally {
      send.disabled = false;
      statusEl.textContent = '';
      input.focus();
    }
  }

  function pip(label, value, klass) {
    return '<span class="pip ' + (klass || '') + '">'
      + (label ? '<span class="pip-label">' + esc(label) + '</span>' : '')
      + '<span class="pip-val">' + esc(value) + '</span>'
      + '</span>';
  }

  function renderParty(chars) {
    if (!chars || !chars.length) {
      partyList.innerHTML = '<div class="muted" style="text-align:center; font-size:0.82em; font-style:italic">No characters in roster.</div>';
      return;
    }
    partyList.innerHTML = chars.map(c => {
      const pct = c.hp_max > 0 ? Math.max(0, Math.min(100, (c.hp / c.hp_max) * 100)) : 0;
      const ko = (c.hp || 0) <= 0;
      const portrait = c.portrait
        ? '<img src="' + esc(c.portrait) + '" alt="' + esc(c.label) + '">'
        : '<span>' + esc((c.label || '?').slice(0, 1).toUpperCase()) + '</span>';
      const conds = (c.conditions || []).map(x =>
        '<span class="cond-chip">' + esc(x) + '</span>').join('');
      const cls = [c.cls, c.level].filter(Boolean).join(' ').trim();
      const sheetUrl = '/sheets/' + encodeURIComponent(c.key || '');
      return ''
        + '<article class="party-mini">'
        +   '<a class="portrait-frame ' + (ko ? 'kod ' : '') + (c.portrait ? '' : 'empty') + '" href="' + sheetUrl + '" title="Open ' + esc(c.label) + '\\u2019s sheet">' + portrait + '</a>'
        +   '<div class="party-mini-body">'
        +     '<a class="party-mini-name" href="' + sheetUrl + '">' + esc(c.label) + '</a>'
        +     (cls ? '<div class="party-mini-class">' + esc(cls) + '</div>' : '')
        +     '<div class="party-mini-hp">'
        +       '<span class="hp-meter"><i style="width:' + pct.toFixed(1) + '%"></i></span>'
        +       '<span class="party-mini-hp-text">' + esc(c.hp) + '/' + esc(c.hp_max) + '</span>'
        +     '</div>'
        +     (conds ? '<div class="party-mini-conds">' + conds + '</div>' : '')
        +   '</div>'
        + '</article>';
    }).join('');
  }

  function renderSidebarStats(s) {
    sideCamp.innerHTML = (s.campaign && s.campaign.name) ? esc(s.campaign.name) : '';
    const rows = [];
    const r1 = [];
    if (s.session) r1.push(pip('Sess', s.session));
    if (s.day) r1.push(pip('Day', s.day));
    if (s.time) r1.push(pip('Time', s.time));
    if (r1.length) rows.push('<div class="side-row">' + r1.join('') + '</div>');
    const r2 = [];
    if (s.weather) r2.push(pip('Sky', s.weather));
    if (s.coin) r2.push(pip('Gold', s.coin, 'pip-gold'));
    if (r2.length) rows.push('<div class="side-row">' + r2.join('') + '</div>');
    if (s.location) {
      rows.push('<div class="side-row row-loc">' + pip('At', s.location) + '</div>');
    }
    if (s.in_combat) {
      rows.push('<div class="side-row row-combat">' + pip('', '⚔ In Combat') + '</div>');
    }
    sideStats.innerHTML = rows.join('');
  }

  async function refreshState() {
    try {
      const r = await fetch('/api/play_state.json');
      if (!r.ok) return;
      const s = await r.json();
      renderParty(s.characters);
      renderSidebarStats(s);
    } catch (e) {
      // best-effort: leave existing render in place
    }
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    submitTurn(text);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      form.requestSubmit();
    }
  });
  resetBtn.addEventListener('click', async () => {
    if (!confirm('Start a new DM session? The current conversation will no longer be resumed.')) return;
    await fetch('/play/reset', {method: 'POST'});
    sidEl.textContent = '(new)';
    append('meta', 'new session started');
  });

  makePicBtn.addEventListener('click', async () => {
    if (!lastDmText) {
      statusEl.textContent = 'no DM text yet';
      setTimeout(() => { statusEl.textContent = ''; }, 2000);
      return;
    }
    if (firstAppend) { log.innerHTML = ''; firstAppend = false; }
    const placeholder = document.createElement('div');
    placeholder.className = 'turn generating';
    placeholder.textContent = 'illustrating the scene…';
    log.appendChild(placeholder);
    log.scrollTop = log.scrollHeight;
    makePicBtn.disabled = true;
    try {
      const r = await fetch('/play/generate_scene', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({prompt: lastDmText.slice(0, 1500)}),
      });
      const d = await r.json();
      if (!r.ok || d.error) {
        placeholder.className = 'turn error';
        placeholder.textContent = 'image generation failed: ' + (d.error || ('HTTP ' + r.status));
        return;
      }
      placeholder.className = 'turn scene';
      placeholder.innerHTML = '<img src="' + esc(d.url) + '" alt="generated scene"><span class="caption">scene</span>';
      log.scrollTop = log.scrollHeight;
    } catch (err) {
      placeholder.className = 'turn error';
      placeholder.textContent = 'image generation failed: ' + String(err);
    } finally {
      makePicBtn.disabled = false;
    }
  });

  // Filter pills: persist Show/Hide per turn-type in localStorage.
  document.querySelectorAll('.turn-toggle').forEach(btn => {
    const key = btn.dataset.filter;
    const hidden = localStorage.getItem('play.hide.' + key) === '1';
    btn.classList.toggle('active', !hidden);
    log.classList.toggle('hide-' + key, hidden);
    btn.addEventListener('click', () => {
      const wasActive = btn.classList.contains('active');
      const nowActive = !wasActive;
      btn.classList.toggle('active', nowActive);
      log.classList.toggle('hide-' + key, !nowActive);
      localStorage.setItem('play.hide.' + key, nowActive ? '0' : '1');
    });
  });

  fetch('/play/session').then(r => r.json()).then(d => {
    sidEl.textContent = d.session_id ? d.session_id.slice(0, 8) : '(new)';
  });

  // Replay the most recent DM reply from the resumed session, if any.
  fetch('/api/last_narrative').then(r => r.json()).then(d => {
    if (d && d.text) {
      append('dm', d.text);
      lastDmText = d.text;
    }
  }).catch(() => {});

  refreshState();
  // Background refresh — picks up MCP-tool mutations made outside the turn flow.
  setInterval(refreshState, 20000);

  input.focus();
})();
</script>
"""


@app.route("/play")
def play_page():
    return render("Play", _PLAY_HTML)


@app.route("/play/session")
def play_session_id():
    return jsonify({"session_id": _dm.session_id()})


@app.route("/play/reset", methods=["POST"])
def play_reset():
    _dm.reset_session()
    return jsonify({"ok": True})


@app.route("/api/play_state.json")
def api_play_state():
    return jsonify(_play_state())


@app.route("/api/last_narrative")
def api_last_narrative():
    """Return the most recent DM text from the resumed Claude Code session,
    so the /play surface can replay it on reload. Returns {"text": null} when
    there's no session, no transcript, or no recorded DM prose yet."""
    return jsonify({"text": _dm.last_dm_text()})


@app.route("/play/turn", methods=["POST"])
def play_turn():
    data = request.get_json(silent=True) or {}
    msg = (data.get("message") or "").strip()
    if not msg:
        return jsonify({"error": "empty message"}), 400

    def gen():
        for evt in _dm.stream_turn(msg):
            yield json.dumps(evt) + "\n"

    resp = Response(gen(), mimetype="application/x-ndjson")
    # Defeat any reverse-proxy buffering (nginx etc.); harmless on the dev server.
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/play/generate_scene", methods=["POST"])
def play_generate_scene():
    """Generate an illustration based on a free-form prompt (used by the
    /play 'Make picture' button to render the previous DM reply as art).
    Synchronous — returns once the image is downloaded. Slow (~10–30s)."""
    if not cfg:
        return jsonify({"error": "no campaign loaded"}), 400
    data = request.get_json(silent=True) or {}
    prompt = (data.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "empty prompt"}), 400
    # Trim very long prompts — Replicate has its own limit and shorter often
    # composes better.
    prompt = prompt[:1500]

    try:
        replicate = _img._load_replicate()
    except (ImportError, EnvironmentError) as exc:
        return jsonify({"error": str(exc)}), 500

    tone = cfg.get("tone", "high fantasy")
    tone_clause = _img.TONE_STYLES.get(tone, tone)
    full_prompt = f"{_img.STYLE_BASE} {tone_clause}. {prompt}"

    title = "Live play scene"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"{ts}_{_img._safe_slug(title)}.png"
    images_dir = cfg["_data_dir"] / "images"

    try:
        _img._generate_image(replicate, full_prompt, images_dir, filename)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    _img._update_index(images_dir, {
        "filename":    filename,
        "scene":       title,
        "description": prompt[:500],
        "timestamp":   datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type":        "scene",
    })
    return jsonify({"url": f"/images/{filename}", "filename": filename})



def _encumbrance_html(char: dict, state: dict, key: str) -> str:
    """Render a 'Carrying' block: total weight, band, and STR-keyed thresholds.
    Returns an empty string if the character has no usable ability scores."""
    ab = char.get("ability_scores") or {}
    if "str" not in ab:
        return ""

    enc = _encumbrance_band(char, state, key)
    band = enc["band"]
    weight = enc["weight"]
    penalty = enc["penalty"]
    th = enc["thresholds"]
    strength = enc["strength"]

    band_color = {
        "light":      "#6db86d",
        "moderate":   "#c8a96e",
        "heavy":      "#e0a060",
        "severe":     "#e08060",
        "overloaded": "#ff6a4a",
    }.get(band, "#c8a96e")
    penalty_str = f" &nbsp;·&nbsp; {penalty:+d} move" if penalty else ""

    # Bar — fraction of severe (overloaded cap). Cap at 1.0.
    severe_cap = max(th["severe"], 1)
    pct = min(100.0, weight / severe_cap * 100.0)
    pct_color = band_color

    rows = []
    for tname in ("light", "moderate", "heavy", "severe"):
        cap = th[tname]
        active = " enc-band-active" if band == tname else ""
        rows.append(
            f'<div class="enc-band{active}">'
            f'<span class="enc-band-name">{tname}</span>'
            f'<span class="enc-band-cap">≤ {cap} lb</span>'
            f'</div>'
        )

    # Per-item breakdown
    item_rows = []
    for it in enc["items"]:
        name = html.escape(it["item"])
        qty = it["qty"]
        unit = it["unit_lb"]
        sub = it["weight_lb"]
        wsrc = it.get("source", "unknown")

        if wsrc == "unknown":
            unit_cell = '<span class="muted" title="not found in 2e item DB or PHB">— lb</span>'
            sub_cell  = '<span class="muted">—</span>'
        elif wsrc == "negligible":
            unit_cell = '<span class="muted" title="PHB lists this as negligible (10/lb)">≈ 0</span>'
            sub_cell  = '<span class="muted">—</span>'
        else:
            unit_cell = f'{unit:g} lb'
            sub_cell  = f'<strong>{sub:g} lb</strong>'

        src_chips = []
        if it.get("origin") == "consumable":
            src_chips.append('<span class="tag" style="font-size:.7em">consumable</span>')
        if wsrc == "phb":
            src_chips.append('<span class="tag" style="font-size:.7em;background:#1a2a3a;color:#a0c0e0;border-color:#3a5a7a" title="weight from PHB Chapter 6">PHB</span>')
        elif wsrc == "alias":
            src_chips.append('<span class="tag" style="font-size:.7em" title="resolved via alias to a canonical 2e item">alias</span>')
        chips_html = " ".join(src_chips)

        qty_cell = f'×{qty}' if qty > 1 else ''
        item_rows.append(
            f'<tr>'
            f'<td>{name} {chips_html}</td>'
            f'<td style="text-align:right">{qty_cell}</td>'
            f'<td style="text-align:right">{unit_cell}</td>'
            f'<td style="text-align:right">{sub_cell}</td>'
            f'</tr>'
        )
    item_table = ""
    if item_rows:
        item_table = (
            '<table class="enc-items">'
            '<thead><tr>'
            '<th>Item</th>'
            '<th style="text-align:right">Qty</th>'
            '<th style="text-align:right">Unit</th>'
            '<th style="text-align:right">Weight</th>'
            '</tr></thead>'
            f'<tbody>{"".join(item_rows)}</tbody>'
            '<tfoot><tr>'
            '<td colspan="3" style="text-align:right"><strong>Total</strong></td>'
            f'<td style="text-align:right"><strong>{weight} lb</strong></td>'
            '</tr></tfoot>'
            '</table>'
        )
    else:
        item_table = ('<p class="muted" style="font-size:.85em">'
                      'No items recorded.</p>')

    return (
        f'<div class="card enc-card">'
        f'<h3 style="margin-top:0">Carrying</h3>'
        f'<p><strong>Weight:</strong> {weight} lb '
        f'&nbsp;·&nbsp; <span style="color:{band_color}"><strong>{band}</strong></span>'
        f'{penalty_str} '
        f'<span class="muted">(STR {strength})</span></p>'
        f'<div class="enc-bar"><div class="enc-fill" '
        f'style="width:{pct:.1f}%;background:{pct_color}"></div></div>'
        f'<div class="enc-bands">{"".join(rows)}</div>'
        f'{item_table}'
        f'<p class="muted" style="font-size:.8em;margin-top:6px">'
        f'Sources: 2e item DB · <span class="tag" style="font-size:.7em">alias</span> matched to canonical name · '
        f'<span class="tag" style="font-size:.7em;background:#1a2a3a;color:#a0c0e0;border-color:#3a5a7a">PHB</span> '
        f'sourced from PHB Chapter 6 · '
        f'“≈ 0” means PHB lists the item as negligible (10 per pound) · '
        f'“— lb” means no match — override with <code>item_update</code>.</p>'
        f'</div>'
    )


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
        desc_block = (
            f'<details class="portrait-desc"><summary>portrait notes</summary>'
            f'<p>{desc}</p></details>'
        ) if desc else ""
        portrait_html = (
            f'<div style="float:right;margin:0 0 16px 20px;max-width:200px;text-align:center">'
            f'<img src="/images/{fn}" alt="{html.escape(name)}" '
            f'style="width:100%;border-radius:3px;border:1px solid var(--rule-hi);box-shadow:var(--shadow-card)">'
            f'{desc_block}'
            f'</div>'
        )

    # Encumbrance block — only for characters known to the campaign config
    char = cfg.get("characters", {}).get(slug) or cfg.get("npcs", {}).get(slug)
    enc_html = ""
    if char:
        state = _c.load_state(cfg)
        enc_html = _encumbrance_html(char, state, slug)

    body = (
        f'<div class="card clearfix">'
        f"{portrait_html}"
        f"{content_html}"
        f"</div>"
        f"{enc_html}"
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
    # Collect (name, slug, hit_die, pr_str, is_homebrew) entries from both sources.
    entries: list[tuple[str, str, str, str, bool, str]] = []

    if _2E_DB.exists():
        conn = _db(_2E_DB)
        for r in conn.execute(
            "SELECT name, hit_die, prime_requisite FROM classes ORDER BY name"
        ).fetchall():
            slug = re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
            try:
                pr = json.loads(r["prime_requisite"] or "[]")
                pr_str = ", ".join(p.capitalize() for p in pr) if pr else "—"
            except (json.JSONDecodeError, TypeError):
                pr_str = str(r["prime_requisite"] or "—")
            entries.append((r["name"], slug, r["hit_die"] or "—", pr_str, False, ""))

    for hb in _hb.list_homebrew():
        if hb.get("_error"):
            entries.append((
                hb.get("name", "?"), hb.get("slug", "?"),
                "—", "—", True,
                f'<span class="tag" style="background:#3a1818;color:#c87a7a">load error</span>',
            ))
            continue
        try:
            pr = json.loads(hb.get("prime_requisite") or "[]")
            pr_str = ", ".join(p.capitalize() for p in pr) if pr else "—"
        except (json.JSONDecodeError, TypeError):
            pr_str = "—"
        entries.append((
            hb["name"], hb["slug"], hb.get("hit_die") or "—",
            pr_str, True, "",
        ))

    if not entries:
        body = "<h1>Classes</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
        return render("Classes", body)

    entries.sort(key=lambda e: e[0].lower())

    cards = []
    for name, slug, hit_die, pr_str, is_homebrew, extra in entries:
        tag = ('<span class="tag" style="background:#1a3a18;color:#6db86d;'
               'border-color:#3a6a3a">homebrew</span>' if is_homebrew else "")
        cards.append(
            f'<div class="card" style="padding:10px 14px">'
            f'<h2 style="margin:0 0 2px">'
            f'<a href="/classes/{html.escape(slug)}">{html.escape(name)}</a> {tag}{extra}</h2>'
            f'<p class="muted" style="margin:0;font-size:.9em">'
            f'Hit Die: {html.escape(hit_die)} &nbsp;·&nbsp; '
            f'Prime Requisite: {html.escape(pr_str)}'
            f'</p></div>'
        )

    body = "<h1>Classes</h1>" + "".join(cards)
    return render("Classes", body)


@app.route("/classes/<slug>")
def class_detail(slug: str):
    # Try homebrew first; the JSON files are authoritative for any class they
    # define, so a homebrew Barbarian wins over any same-named PHB entry.
    cls_row: dict | None = None
    level_rows: list = []
    is_homebrew = False
    homebrew_extras: dict = {}

    hb = _hb.get_homebrew(slug)
    if hb is not None and not hb.get("_error"):
        cls_row = hb
        level_rows = hb.get("level_rows", [])
        is_homebrew = True
        homebrew_extras = {k: hb.get(k) for k in (
            "ability_requirements", "allowed_races", "allowed_alignments",
            "allowed_armor", "weapon_specialization", "casts_spells",
            "base_movement_rate", "weapon_proficiency_slots",
            "nonweapon_proficiency_slots", "progression_tables",
            "source", "source_url",
        ) if hb.get(k) is not None}
    elif _2E_DB.exists():
        name_pattern = slug.replace("-", " ")
        conn = _db(_2E_DB)
        cls_row = conn.execute(
            "SELECT * FROM classes WHERE lower(replace(name,' ','-')) = ? "
            "OR name LIKE ? COLLATE NOCASE LIMIT 1",
            (slug, f"%{name_pattern}%"),
        ).fetchone()
        if cls_row is not None:
            level_rows = conn.execute(
                "SELECT * FROM class_levels WHERE class_id=? ORDER BY level",
                (cls_row["id"],),
            ).fetchall()

    if cls_row is None:
        abort(404)

    def _g(row, key):
        try:
            return row[key]
        except (KeyError, IndexError):
            return None

    try:
        pr = json.loads(_g(cls_row, "prime_requisite") or "[]")
        pr_str = ", ".join(str(p).capitalize() for p in pr) if pr else "—"
    except (json.JSONDecodeError, TypeError):
        pr_str = str(_g(cls_row, "prime_requisite") or "—")

    try:
        abilities = json.loads(_g(cls_row, "special_abilities") or "[]")
    except (json.JSONDecodeError, TypeError):
        abilities = []

    # Build XP / progression table
    has_spells = any(_g(r, "spell_slots") for r in level_rows)

    header_cells = "<tr><th>Level</th><th>XP</th><th>HD</th><th>THAC0</th><th>Atk</th>"
    header_cells += "<th>Para</th><th>RSW</th><th>Pet</th><th>Brth</th><th>Spell</th>"
    if has_spells:
        header_cells += "<th>Spell Slots</th>"
    header_cells += "</tr>"

    tbody = ""
    for r in level_rows:
        slots_str = ""
        if has_spells and _g(r, "spell_slots"):
            try:
                slots = json.loads(_g(r, "spell_slots"))
                slots_str = " / ".join(str(v) for k, v in sorted(slots.items()) if v)
            except Exception:
                slots_str = "—"

        xp_v = _g(r, "xp_required")
        xp = f"{xp_v:,}" if xp_v is not None else "—"
        tbody += (
            f"<tr>"
            f"<td>{_g(r, 'level')}</td>"
            f"<td>{xp}</td>"
            f"<td>{html.escape(_g(r, 'hit_dice') or '—')}</td>"
            f"<td>{_g(r, 'thac0') or '—'}</td>"
            f"<td>{html.escape(str(_g(r, 'attacks') or '—'))}</td>"
            f"<td>{_g(r, 'save_paralysis') or '—'}</td>"
            f"<td>{_g(r, 'save_rsw') or '—'}</td>"
            f"<td>{_g(r, 'save_petrify') or '—'}</td>"
            f"<td>{_g(r, 'save_breath') or '—'}</td>"
            f"<td>{_g(r, 'save_spell') or '—'}</td>"
            + (f"<td>{slots_str or '—'}</td>" if has_spells else "")
            + "</tr>"
        )

    prog_table = f"<table><thead>{header_cells}</thead><tbody>{tbody}</tbody></table>"

    abilities_html = ""
    if abilities:
        items = "".join(f"<li>{html.escape(str(a))}</li>" for a in abilities)
        abilities_html = f"<h2>Special Abilities</h2><ul>{items}</ul>"

    desc_html = ""
    if _g(cls_row, "description"):
        paras = [p.strip() for p in (_g(cls_row, "description") or "").split("\n") if p.strip()]
        desc_html = "".join(f"<p>{html.escape(p)}</p>" for p in paras[:4])
        if len(paras) > 4:
            desc_html += f"<p class='muted'>…{len(paras)-4} more paragraphs</p>"

    # Homebrew kit metadata block
    hb_meta_html = ""
    if is_homebrew and homebrew_extras:
        meta_rows = []
        if homebrew_extras.get("ability_requirements"):
            req = ", ".join(f"{k.upper()} {v}" for k, v in homebrew_extras["ability_requirements"].items())
            meta_rows.append(("Ability Requirements", req))
        if homebrew_extras.get("allowed_races"):
            meta_rows.append(("Allowed Races", ", ".join(homebrew_extras["allowed_races"])))
        if homebrew_extras.get("allowed_alignments"):
            meta_rows.append(("Allowed Alignments", ", ".join(homebrew_extras["allowed_alignments"])))
        if homebrew_extras.get("allowed_armor"):
            meta_rows.append(("Allowed Armor", ", ".join(homebrew_extras["allowed_armor"])))
        if homebrew_extras.get("base_movement_rate") is not None:
            meta_rows.append(("Base Movement", str(homebrew_extras["base_movement_rate"])))
        if homebrew_extras.get("weapon_specialization") is not None:
            meta_rows.append(("Weapon Specialization", "yes" if homebrew_extras["weapon_specialization"] else "no"))
        if homebrew_extras.get("casts_spells") is not None:
            meta_rows.append(("Casts Spells", "yes" if homebrew_extras["casts_spells"] else "no"))
        wp = homebrew_extras.get("weapon_proficiency_slots") or {}
        if wp:
            meta_rows.append(("Weapon Profs", f"{wp.get('initial','?')} initial, +1 per {wp.get('advance_every_levels','?')} levels"))
        nwp = homebrew_extras.get("nonweapon_proficiency_slots") or {}
        if nwp:
            meta_rows.append(("Nonweapon Profs", f"{nwp.get('initial','?')} initial, +1 per {nwp.get('advance_every_levels','?')} levels"))
        if meta_rows:
            rows_html = "".join(
                f"<tr><th style='text-align:left'>{html.escape(k)}</th><td>{html.escape(v)}</td></tr>"
                for k, v in meta_rows
            )
            hb_meta_html = f"<h2>Class Restrictions</h2><div class='card'><table>{rows_html}</table></div>"

    # Kit progression tables (climb%, leap, etc.)
    prog_extras_html = ""
    pt = (homebrew_extras.get("progression_tables") or {}) if is_homebrew else {}
    if pt:
        blocks = []
        for tname, tdata in pt.items():
            by_level = tdata.get("by_level") or {}
            if not by_level:
                continue
            sample_val = next(iter(by_level.values()))
            if isinstance(sample_val, dict):
                cols = sorted(sample_val.keys())
                head = "<th>Level</th>" + "".join(f"<th>{html.escape(c.replace('_',' ').title())}</th>" for c in cols)
                rows_h = ""
                for lvl in sorted(by_level, key=lambda k: int(k)):
                    cells = "".join(f"<td>{html.escape(str(by_level[lvl].get(c, '—')))}</td>" for c in cols)
                    rows_h += f"<tr><td>{lvl}</td>{cells}</tr>"
            else:
                head = "<th>Level</th><th>Value</th>"
                rows_h = "".join(
                    f"<tr><td>{lvl}</td><td>{html.escape(str(by_level[lvl]))}</td></tr>"
                    for lvl in sorted(by_level, key=lambda k: int(k))
                )
            desc = tdata.get("description", "")
            blocks.append(
                f"<h3 style='margin-top:14px'>{html.escape(tname.replace('_', ' ').title())}</h3>"
                + (f"<p class='muted'>{html.escape(desc)}</p>" if desc else "")
                + f"<table><thead><tr>{head}</tr></thead><tbody>{rows_h}</tbody></table>"
            )
        if blocks:
            prog_extras_html = (
                f"<h2 style='margin-top:20px'>Kit Progression Tables</h2>"
                f"<div class='card' style='overflow-x:auto'>{''.join(blocks)}</div>"
            )

    # Source citation for homebrew
    src_html = ""
    if is_homebrew:
        src_text = homebrew_extras.get("source") or "homebrew"
        src_url = homebrew_extras.get("source_url") or ""
        if src_url:
            src_html = (f"<p class='muted'>Source: "
                        f"<a href='{html.escape(src_url)}'>{html.escape(src_text)}</a></p>")
        else:
            src_html = f"<p class='muted'>Source: {html.escape(src_text)}</p>"

    homebrew_tag = ('<span class="tag" style="background:#1a3a18;color:#6db86d;'
                    'border-color:#3a6a3a">homebrew</span>' if is_homebrew else "")

    body = (
        f'<p><a href="/classes">← Classes</a></p>'
        f'<h1>{html.escape(_g(cls_row, "name"))} {homebrew_tag}</h1>'
        f'<div class="card">'
        f'<p><strong>Hit Die:</strong> {html.escape(_g(cls_row, "hit_die") or "—")} &nbsp;·&nbsp; '
        f'<strong>Prime Requisite:</strong> {html.escape(pr_str)}</p>'
        + src_html
        + (f"<hr>{desc_html}" if desc_html else "")
        + f'</div>'
        + hb_meta_html
        + f'<h2 style="margin-top:20px">Progression</h2>'
        + f'<div class="card" style="overflow-x:auto">{prog_table}</div>'
        + (f'<div class="card">{abilities_html}</div>' if abilities_html else "")
        + prog_extras_html
    )
    return render(f'{html.escape(_g(cls_row, "name"))} — Classes', body)


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
# World maps
# ---------------------------------------------------------------------------
from tools import world_map as _world_map


@app.route("/maps")
def maps_index():
    """List all world maps in the active campaign + their views."""
    try:
        d = _world_map._maps_dir()
    except Exception as e:
        return render("Maps", f'<h1>World Maps</h1><p style="color:#c66">{html.escape(str(e))}</p>')

    items, fragments = [], []
    for p in sorted(d.glob("*.map")):
        slug = p.stem
        if not _world_map._is_workspace_file(p):
            fragments.append(slug)
            continue
        try:
            ws = _world_map.parse_file(p)
            view_links = " · ".join(
                f'<a href="/maps/{html.escape(slug)}/{html.escape(v.name)}">{html.escape(v.name)}</a>'
                for v in ws.views
            ) or '<span style="color:#8a7a60">no views</span>'
            n_features = len(ws.model)
            items.append(
                f'<li style="margin:6px 0">'
                f'<b style="color:#c8a96e">{html.escape(slug)}</b> '
                f'<span style="color:#8a7a60;font-size:.9em">({n_features} top-level features)</span><br>'
                f'<span style="margin-left:1em">Views: {view_links}</span>'
                f'</li>'
            )
        except Exception as e:
            items.append(
                f'<li><b>{html.escape(slug)}</b> '
                f'<span style="color:#c66">parse error: {html.escape(str(e))}</span></li>'
            )

    if not items and not fragments:
        body = (
            '<h1>World Maps</h1>'
            '<p style="color:#8a7a60">No maps yet. Create one with the MCP tool '
            '<code>create_world_map(slug)</code>, then populate it via '
            '<code>update_world_map</code> or <code>add_world_map_feature</code>.</p>'
        )
    else:
        body = '<h1>World Maps</h1>'
        if items:
            body += '<ul style="list-style:none;padding:0">' + ''.join(items) + '</ul>'
        if fragments:
            frag_html = ', '.join(f'<code>{html.escape(s)}</code>' for s in fragments)
            body += (
                '<h3 style="margin-top:24px">Include fragments</h3>'
                '<p style="color:#8a7a60;font-size:.9em">Files without a top-level <code>workspace</code> block. '
                'These are inlined by other maps via <code>!include</code>: '
                + frag_html + '</p>'
            )
    return render("World Maps", body)


@app.route("/maps/<slug>/<view>")
def map_view(slug, view):
    """Render a Leaflet view of a world map."""
    path = _world_map._maps_dir() / f"{_world_map._safe_slug(slug)}.map"
    if not path.exists():
        abort(404)
    try:
        _world_map.parse_file(path)  # validate before serving
    except SyntaxError as e:
        body = (
            f'<h1>{html.escape(slug)} — {html.escape(view)}</h1>'
            f'<p style="color:#c66">DSL error: {html.escape(str(e))}</p>'
            f'<p><a href="/maps">← back to maps</a></p>'
        )
        return render(f"Map: {slug}", body)

    data_url  = f"/api/maps/{slug}/{view}.geojson"
    party_url = f"/api/maps/{slug}/party.geojson"
    body = (
        '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" '
        'integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin="">'
        '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" '
        'integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>'
        '<style>'
        '  .map-layout { display:flex; gap:12px; align-items:stretch; }'
        '  #worldmap { flex:1; height:75vh; min-width:0; '
        '              background:#1a1510; border:1px solid #4a3828; border-radius:6px; }'
        '  #worldmap-sidebar { width:260px; max-height:75vh; overflow-y:auto; '
        '                      padding:12px; background:#2a2018; border:1px solid #4a3828; border-radius:6px; }'
        '  .leaflet-popup-content { color:#1a1510; font-family:Georgia,serif; }'
        '  .leaflet-control-layers { background:#2a2018; color:#d4c5a9; border:1px solid #4a3828; }'
        '  .leaflet-control-layers-overlays label { color:#d4c5a9; }'
        '</style>'
        f'<h1>{html.escape(slug)} <span style="color:#8a7a60;font-size:.7em">— {html.escape(view)}</span></h1>'
        '<p style="color:#8a7a60;font-size:.9em"><a href="/maps">← all maps</a></p>'
        '<div class="map-layout">'
        '  <div id="worldmap"></div>'
        '  <div id="worldmap-sidebar">Loading…</div>'
        '</div>'
        f'<script>'
        f'window.WORLD_MAP_SLUG  = {json.dumps(slug)};'
        f'window.WORLD_MAP_VIEW  = {json.dumps(view)};'
        f'window.WORLD_MAP_DATA  = {json.dumps(data_url)};'
        f'window.WORLD_MAP_PARTY = {json.dumps(party_url)};'
        f'</script>'
        '<script src="/static/world-map.js"></script>'
    )
    return render(f"Map: {slug}/{view}", body)


@app.route("/api/maps/<slug>/<view>.geojson")
def map_geojson(slug, view):
    """Compile-on-demand GeoJSON for a world-map view."""
    path = _world_map._maps_dir() / f"{_world_map._safe_slug(slug)}.map"
    if not path.exists():
        return jsonify({"error": f"map '{slug}' not found"}), 404
    try:
        ws = _world_map.parse_file(path)
        fc = _world_map.compile_view(ws, view)
    except (SyntaxError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(fc)


@app.route("/api/maps/<slug>/party.geojson")
def map_party_geojson(slug):
    """Serve the party-position overlay if it exists (404 otherwise)."""
    overlay = _world_map._maps_dir() / f"{_world_map._safe_slug(slug)}.party.geojson"
    if not overlay.exists():
        return jsonify({"error": "no party overlay"}), 404
    return Response(overlay.read_text(encoding="utf-8"), mimetype="application/json")


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
