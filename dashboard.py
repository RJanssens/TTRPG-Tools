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

# ---------------------------------------------------------------------------
# Global state — set in main()
# ---------------------------------------------------------------------------
cfg: dict = {}
app = Flask(__name__)

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
</style>
</head>
<body>
<nav>
  <a href="/">Gallery</a>
  <a href="/party">Party</a>
  <a href="/log">Chronicle</a>
  <a href="/characters">Characters</a>
  <a href="/locations">Locations</a>
  <a href="/monsters">Monsters</a>
  <a href="/spells">Spells</a>
  <a href="/classes">Classes</a>
  <a href="/combat">Combat</a>
</nav>
<div class="container">
{body}
</div>
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

    body = (
        f"<h1>{html.escape(campaign_name)} — Party</h1>"
        f"<p class='muted'>Day {day} &nbsp;·&nbsp; Session {session} &nbsp;·&nbsp; Coin: {html.escape(coin_str)}</p>"
        + "".join(cards)
    )
    return render(f"{campaign_name} — Party", body)


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


# ---------------------------------------------------------------------------
# Monsters
# ---------------------------------------------------------------------------

def _monster_row_to_dict(row) -> dict:
    keys = ["id","name","frequency","no_appearing","armor_class","move","hit_dice",
            "thac0","pct_in_lair","treasure_type","no_of_attacks","damage_attack",
            "special_attacks","special_defenses","magic_resistance","intelligence",
            "alignment","size","psionic_ability","attack_defense_modes","description",
            "climate_terrain","morale","xp_value"]
    return {k: row[k] for k in keys if k in row.keys()}


@app.route("/monsters/portraits/<filename>")
def serve_monster_portrait(filename: str):
    return send_from_directory(str(_MONSTERS_DIR), filename)


@app.route("/monsters/<slug>/generate-portrait", methods=["POST"])
def generate_monster_portrait(slug: str):
    if not _MONSTERS_DB.exists():
        return jsonify({"error": "monsters.db not found"}), 404

    conn = sqlite3.connect(str(_MONSTERS_DB))
    conn.row_factory = sqlite3.Row
    name_pattern = slug.replace("-", "%")
    row = conn.execute(
        "SELECT * FROM monsters WHERE replace(lower(name),' ','-') LIKE ? "
        "OR replace(lower(name),' ','%') LIKE ? COLLATE NOCASE LIMIT 1",
        (slug, name_pattern),
    ).fetchone()
    conn.close()

    if row is None:
        return jsonify({"error": "Monster not found"}), 404

    m = _monster_row_to_dict(row)
    name = m["name"]

    desc = (m.get("description") or "").strip()
    prompt_parts = [f"Full body illustration of a {name}"]
    if m.get("size") and str(m["size"]).upper() not in ("M", "MEDIUM"):
        prompt_parts.append(f"{m['size'].lower()}-sized creature")
    if desc:
        prompt_parts.append(desc)
    prompt = ". ".join(prompt_parts)

    try:
        from tools.images import _load_replicate, _generate_image, STYLE_BASE, TONE_STYLES
        replicate = _load_replicate()
    except (ImportError, EnvironmentError) as exc:
        return jsonify({"error": str(exc)}), 500

    tone = cfg.get("tone", "high fantasy")
    tone_clause = TONE_STYLES.get(tone, tone)
    full_prompt = f"{STYLE_BASE} {tone_clause}. {prompt}"

    _MONSTERS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{slug}.png"
    try:
        _generate_image(replicate, full_prompt, _MONSTERS_DIR, filename)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"filename": filename, "slug": slug})


@app.route("/monsters")
def monsters():
    q = request.args.get("q", "").strip()
    campaign_name = cfg.get("name", "Campaign")

    if not _MONSTERS_DB.exists():
        body = "<h1>Monsters</h1><p class='muted'>monsters.db not found.</p>"
        return render("Monsters", body)

    conn = sqlite3.connect(str(_MONSTERS_DB))
    conn.row_factory = sqlite3.Row

    if q:
        rows = conn.execute(
            "SELECT * FROM monsters WHERE name LIKE ? COLLATE NOCASE ORDER BY name",
            (f"%{q}%",),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM monsters ORDER BY name").fetchall()
    conn.close()

    search_box = (
        '<form method="get" style="margin-bottom:16px">'
        '<input name="q" value="{q}" placeholder="Search monsters…" '
        'style="background:#2a2018;border:1px solid #4a3828;color:#d4c5a9;'
        'padding:6px 10px;border-radius:4px;width:300px;font-size:1em">'
        ' <button type="submit" style="background:#3a2818;border:1px solid #5a4030;'
        'color:#c8a96e;padding:6px 14px;border-radius:4px;cursor:pointer">Search</button>'
        '</form>'
    ).format(q=html.escape(q))

    if not rows:
        body = f"<h1>Monsters</h1>{search_box}<p class='muted'>No results for '{html.escape(q)}'.</p>"
        return render("Monsters", body)

    portraits = _monster_portraits_dict()

    cards = []
    for r in rows:
        slug = re.sub(r"[^a-z0-9]+", "-", r["name"].lower()).strip("-")
        portrait_icon = ""
        if slug in portraits:
            fn = html.escape(portraits[slug])
            portrait_icon = (
                f' <img src="/monsters/portraits/{fn}" alt="" loading="lazy" '
                f'style="width:32px;height:32px;object-fit:cover;border-radius:3px;'
                f'vertical-align:middle;margin-left:6px;border:1px solid #4a3828">'
            )
        cards.append(
            f'<div class="card" style="padding:10px 14px">'
            f'<h2 style="margin:0 0 4px">'
            f'<a href="/monsters/{html.escape(slug)}">{html.escape(r["name"])}</a>'
            f'{portrait_icon}</h2>'
            f'<p class="muted" style="margin:0;font-size:.9em">'
            f'HD&nbsp;{html.escape(str(r["hit_dice"] or "—"))} &nbsp;·&nbsp; '
            f'AC&nbsp;{html.escape(str(r["armor_class"] or "—"))} &nbsp;·&nbsp; '
            f'THAC0&nbsp;{html.escape(str(r["thac0"] or "—"))} &nbsp;·&nbsp; '
            f'Dmg&nbsp;{html.escape(str(r["damage_attack"] or "—"))} &nbsp;·&nbsp; '
            f'{html.escape(str(r["alignment"] or "—"))}'
            f'</p></div>'
        )

    count_note = f"<p class='muted' style='margin-bottom:12px'>{len(rows)} monster{'s' if len(rows)!=1 else ''}</p>"
    body = f"<h1>Monsters</h1>{search_box}{count_note}" + "".join(cards)
    return render("Monsters", body)


@app.route("/monsters/<slug>")
def monster_detail(slug: str):
    # Convert slug back to a name pattern for lookup
    name_pattern = slug.replace("-", "%")

    if not _MONSTERS_DB.exists():
        abort(404)

    conn = sqlite3.connect(str(_MONSTERS_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM monsters WHERE replace(lower(name),' ','-') LIKE ? "
        "OR replace(lower(name),' ','%') LIKE ? COLLATE NOCASE LIMIT 1",
        (slug, name_pattern),
    ).fetchone()
    conn.close()

    if row is None:
        abort(404)

    m = _monster_row_to_dict(row)
    name = m["name"]

    def stat(label, val):
        if not val or val == "None":
            return ""
        return (
            f'<tr><th style="width:170px;white-space:nowrap">{html.escape(label)}</th>'
            f'<td>{html.escape(str(val))}</td></tr>'
        )

    desc_html = ""
    if m.get("description"):
        paras = [p.strip() for p in m["description"].split("\n") if p.strip()]
        desc_html = "".join(f"<p>{html.escape(p)}</p>" for p in paras)

    table = (
        "<table>"
        + stat("Frequency",        m["frequency"])
        + stat("No. Appearing",    m["no_appearing"])
        + stat("Climate/Terrain",  m["climate_terrain"])
        + stat("Armour Class",     m["armor_class"])
        + stat("Movement",         m["move"])
        + stat("Hit Dice",         m["hit_dice"])
        + stat("THAC0",            m["thac0"])
        + stat("No. of Attacks",   m["no_of_attacks"])
        + stat("Damage/Attack",    m["damage_attack"])
        + stat("Special Attacks",  m["special_attacks"])
        + stat("Special Defenses", m["special_defenses"])
        + stat("Magic Resistance", m["magic_resistance"])
        + stat("Intelligence",     m["intelligence"])
        + stat("Alignment",        m["alignment"])
        + stat("Size",             m["size"])
        + stat("Morale",           m["morale"])
        + stat("% in Lair",        m["pct_in_lair"])
        + stat("Treasure Type",    m["treasure_type"])
        + stat("XP Value",         m["xp_value"])
        + "</table>"
    )

    portrait_path = _monster_portrait_path(slug)
    portrait_html = ""
    if portrait_path:
        fn = html.escape(portrait_path.name)
        portrait_html = (
            f'<div style="text-align:center;margin-bottom:16px">'
            f'<img src="/monsters/portraits/{fn}" alt="{html.escape(name)}" '
            f'style="max-width:320px;width:100%;border-radius:6px;border:1px solid #4a3828">'
            f'</div>'
        )

    esc_slug = html.escape(slug)
    btn_label = "Regenerate Portrait" if portrait_path else "Generate Portrait"
    gen_button = (
        f'<div style="margin:0 0 12px">'
        f'<button id="gen-portrait-btn" '
        f'onclick="generateMonsterPortrait(\'{esc_slug}\')" '
        f'style="background:#3a2818;border:1px solid #5a4030;color:#c8a96e;'
        f'padding:6px 16px;border-radius:4px;cursor:pointer;font-size:0.9em">'
        f'{btn_label}</button>'
        f'<span id="gen-portrait-status" style="margin-left:10px;font-size:0.85em;color:#8a7a60"></span>'
        f'</div>'
        f'<script>'
        f'async function generateMonsterPortrait(slug){{'
        f'  const btn=document.getElementById("gen-portrait-btn");'
        f'  const status=document.getElementById("gen-portrait-status");'
        f'  btn.disabled=true; btn.textContent="Generating…";'
        f'  try{{'
        f'    const r=await fetch("/monsters/"+slug+"/generate-portrait",{{method:"POST"}});'
        f'    const d=await r.json();'
        f'    if(d.error){{status.textContent="Error: "+d.error;btn.disabled=false;btn.textContent="{btn_label}";  }}'
        f'    else{{location.reload();}}'
        f'  }}catch(e){{status.textContent="Request failed";btn.disabled=false;btn.textContent="{btn_label}";}}'
        f'}}'
        f'</script>'
    )

    body = (
        f'<p><a href="/monsters">← Monsters</a></p>'
        f"<h1>{html.escape(name)}</h1>"
        f"{portrait_html}"
        f"{gen_button}"
        f'<div class="card">{table}</div>'
        + (f'<div class="card" style="margin-top:12px">{desc_html}</div>' if desc_html else "")
    )
    return render(f"{html.escape(name)} — Monsters", body)


# ---------------------------------------------------------------------------
# Spells
# ---------------------------------------------------------------------------

_SPELL_SCHOOLS = [
    "Abjuration", "Alteration", "Conjuration", "Divination",
    "Enchantment", "Evocation", "Illusion", "Necromancy",
]

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


@app.route("/spells")
def spells():
    q      = request.args.get("q", "").strip()
    caster = request.args.get("caster", "").strip().lower()
    level  = request.args.get("level", "").strip()
    school = request.args.get("school", "").strip()

    if not _2E_DB.exists():
        body = "<h1>Spells</h1><p class='muted'>2e.db not found. Run tools/build_2e_db.py.</p>"
        return render("Spells", body)

    conn = sqlite3.connect(str(_2E_DB))
    conn.row_factory = sqlite3.Row

    where, params = ["1=1"], []
    if q:
        where.append("name LIKE ? COLLATE NOCASE")
        params.append(f"%{q}%")
    if caster:
        where.append("lower(caster) = ?")
        params.append(caster)
    if level.isdigit():
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
    conn.close()

    # Build filter form
    school_opts = '<option value="">All schools</option>' + "".join(
        f'<option value="{s}" {"selected" if school == s else ""}>{s}</option>'
        for s in _SPELL_SCHOOLS
    )
    level_opts = '<option value="">All levels</option>' + "".join(
        f'<option value="{i}" {"selected" if level == str(i) else ""}>Level {i}</option>'
        for i in range(1, 10)
    )
    def _radio(val, label, current):
        chk = "checked" if current == val else ""
        return (
            f'<label style="display:inline-flex;align-items:center;gap:4px;'
            f'cursor:pointer;color:#d4c5a9;font-size:.95em">'
            f'<input type="radio" name="caster" value="{val}" {chk} '
            f'style="accent-color:#c8a96e"> {label}</label>'
        )

    radios = (
        f'<div style="display:flex;gap:14px;align-items:center;'
        f'background:#2a2018;border:1px solid #4a3828;border-radius:4px;padding:6px 12px">'
        + _radio("", "Both", caster)
        + _radio("wizard", "Wizard", caster)
        + _radio("priest", "Priest", caster)
        + f'</div>'
    )

    search_box = (
        f'<form method="get" style="margin-bottom:20px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">'
        f'<input name="q" value="{html.escape(q)}" placeholder="Search by name…" {INPUT_STYLE}>'
        f'{radios}'
        f'<select name="school" {SELECT_STYLE}>{school_opts}</select>'
        f'<select name="level" {SELECT_STYLE}>{level_opts}</select>'
        f'<button type="submit" {BTN_STYLE}>Filter</button>'
        f'</form>'
    )

    if not rows:
        body = f"<h1>Spells</h1>{search_box}<p class='muted'>No results.</p>"
        return render("Spells", body)

    # Separate into wizard / priest when no caster filter is active
    if not caster:
        wizard_rows  = [r for r in rows if (r["caster"] or "").lower() == "wizard"]
        priest_rows  = [r for r in rows if (r["caster"] or "").lower() == "priest"]
        other_rows   = [r for r in rows if (r["caster"] or "").lower() not in ("wizard", "priest")]
        total = len(rows)
        sections = (
            _spell_section("Wizard Spells", wizard_rows)
            + _spell_section("Priest Spells", priest_rows)
            + _spell_section("Other", other_rows)
        )
        body = (
            f"<h1>Spells</h1>{search_box}"
            f"<p class='muted' style='margin-bottom:4px'>{total} spell{'s' if total != 1 else ''}</p>"
            f"{sections}"
        )
    else:
        title = "Wizard Spells" if caster == "wizard" else "Priest Spells"
        body = (
            f"<h1>{title}</h1>{search_box}"
            f"<p class='muted' style='margin-bottom:4px'>{len(rows)} spell{'s' if len(rows) != 1 else ''}</p>"
            + "".join(_spell_card(r) for r in rows)
        )

    return render("Spells", body)


@app.route("/spells/<int:spell_id>")
def spell_detail(spell_id: int):
    if not _2E_DB.exists():
        abort(404)

    conn = sqlite3.connect(str(_2E_DB))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM spells WHERE id=?", (spell_id,)).fetchone()
    conn.close()

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

    desc_html = ""
    if row["description"]:
        paras = [p.strip() for p in row["description"].split("\n") if p.strip()]
        desc_html = "".join(f"<p>{html.escape(p)}</p>" for p in paras)

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

    conn = sqlite3.connect(str(_2E_DB))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, name, hit_die, prime_requisite FROM classes ORDER BY name").fetchall()
    conn.close()

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
    conn = sqlite3.connect(str(_2E_DB))
    conn.row_factory = sqlite3.Row
    cls_row = conn.execute(
        "SELECT * FROM classes WHERE lower(replace(name,' ','-')) = ? "
        "OR name LIKE ? COLLATE NOCASE LIMIT 1",
        (slug, f"%{name_pattern}%"),
    ).fetchone()

    if cls_row is None:
        conn.close()
        abort(404)

    level_rows = conn.execute(
        "SELECT * FROM class_levels WHERE class_id=? ORDER BY level",
        (cls_row["id"],),
    ).fetchall()
    conn.close()

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


# ---------------------------------------------------------------------------
# Combat map
# ---------------------------------------------------------------------------

_COMBAT_CSS = """
<style>
.combat-layout{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}
.combat-canvas-wrap{flex:1 1 auto;overflow-x:auto;min-width:0}
#cmap{display:block;cursor:crosshair;border:1px solid #4a3828;border-radius:4px}
.combat-sidebar{flex:0 0 200px;width:200px}
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
    'map.onHover = function(x, y) {\n'
    '  document.getElementById("coords").textContent = "("+x+", "+y+") — click to copy";\n'
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
        + '<div style="display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;margin-bottom:12px">'
        + '  <span id="round-info" style="color:#c8a96e;font-weight:bold">Loading…</span>'
        + '  <span id="turn-info" style="color:#d4c5a9"></span>'
        + '  <span id="coords" style="color:#8a7a60;font-size:.85em;margin-left:auto">'
        + 'hover map for coordinates</span>'
        + '</div>'
        + '<div class="combat-layout">'
        + '  <div class="combat-canvas-wrap">'
        + '    <canvas id="cmap"></canvas>'
        + '    <div id="unplaced" style="margin-top:6px"></div>'
        + '  </div>'
        + '  <div class="combat-sidebar">'
        + '    <h3 style="margin:0 0 8px;font-size:.95em;color:#c8a96e">Initiative Order</h3>'
        + '    <div id="init-list"></div>'
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
