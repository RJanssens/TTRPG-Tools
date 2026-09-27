"""Monster browser routes for the dashboard.

Extracted from dashboard.py — registers /monsters, /monsters/<slug>,
/monsters/portraits/<filename>, and /monsters/<slug>/generate-portrait.
"""
import html
import json
import re
from pathlib import Path

from flask import abort, jsonify, request, send_from_directory


_MONSTERS_PER_PAGE = 60


def _slugify(name: str) -> str:
    """Monster name -> URL/file slug. MUST match the slug the listing builds
    and the image-filename convention, so links round-trip."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def _resolve_by_slug(conn, slug: str):
    """Find the monster row whose slugified name equals `slug`.

    Slugs collapse spaces, parentheses, commas, apostrophes, etc. to dashes,
    which a SQL `replace(name,' ','-')` can't reverse — so we slugify each name
    in Python (cheap: a name-only scan, then one fetch by id)."""
    for r in conn.execute("SELECT id, name FROM monsters"):
        if _slugify(r["name"]) == slug:
            return conn.execute("SELECT * FROM monsters WHERE id = ?", (r["id"],)).fetchone()
    return None


def _monster_row_to_dict(row) -> dict:
    keys = ["id","name","frequency","no_appearing","armor_class","move","hit_dice",
            "thac0","pct_in_lair","treasure_type","no_of_attacks","damage_attack",
            "special_attacks","special_defenses","magic_resistance","intelligence",
            "alignment","size","psionic_ability","attack_defense_modes","description",
            "climate_terrain","morale","xp_value","categories","source"]
    return {k: row[k] for k in keys if k in row.keys()}


def init(app, *, get_cfg, render, db_get, _MONSTERS_DB, _MONSTERS_DIR,
         monster_portraits_dict, monster_portrait_path):
    """Register monster routes on `app`. `get_cfg` is called per-request to
    pick up the current campaign config (it's a mutable dict in dashboard.py
    but we accept a getter so the calls happen at request time)."""

    @app.route("/monsters/portraits/<filename>")
    def serve_monster_portrait(filename):
        return send_from_directory(str(_MONSTERS_DIR), filename)

    @app.route("/monsters/<slug>/generate-portrait", methods=["POST"])
    def generate_monster_portrait(slug):
        if not _MONSTERS_DB.exists():
            return jsonify({"error": "monsters.db not found"}), 404

        conn = db_get(_MONSTERS_DB)
        row = _resolve_by_slug(conn, slug)

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

        cfg = get_cfg()
        tone = cfg.get("tone", "high fantasy")
        tone_clause = TONE_STYLES.get(tone, tone)

        custom_prompt = ""
        if request.is_json:
            custom_prompt = ((request.get_json(silent=True) or {}).get("prompt") or "").strip()
        subject = custom_prompt if custom_prompt else prompt
        full_prompt = f"{STYLE_BASE} {tone_clause}. {subject}"

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
        try:
            page = max(1, int(request.args.get("page", "1")))
        except ValueError:
            page = 1

        if not _MONSTERS_DB.exists():
            body = "<h1>Monsters</h1><p class='muted'>monsters.db not found.</p>"
            return render("Monsters", body)

        conn = db_get(_MONSTERS_DB)

        live = any(r[1] == "deleted_at"
                   for r in conn.execute("PRAGMA table_info(monsters)"))
        clauses, params = (["deleted_at IS NULL"] if live else []), []
        if q:
            clauses.append("name LIKE ? COLLATE NOCASE")
            params = [f"%{q}%"]
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = conn.execute(f"SELECT COUNT(*) FROM monsters {where}", params).fetchone()[0]

        offset = (page - 1) * _MONSTERS_PER_PAGE
        rows = conn.execute(
            f"SELECT * FROM monsters {where} ORDER BY name LIMIT ? OFFSET ?",
            [*params, _MONSTERS_PER_PAGE, offset],
        ).fetchall()

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

        portraits = monster_portraits_dict()

        cards = []
        for r in rows:
            slug = _slugify(r["name"])
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

        total_pages = max(1, (total + _MONSTERS_PER_PAGE - 1) // _MONSTERS_PER_PAGE)
        pager = ""
        if total_pages > 1:
            q_arg = f"&q={html.escape(q)}" if q else ""
            prev_link = f'<a href="?page={page-1}{q_arg}">‹ prev</a>' if page > 1 else '<span class="muted">‹ prev</span>'
            next_link = f'<a href="?page={page+1}{q_arg}">next ›</a>' if page < total_pages else '<span class="muted">next ›</span>'
            pager = (
                f'<p class="muted" style="margin:16px 0;text-align:center">'
                f'{prev_link} &nbsp; page {page} of {total_pages} &nbsp; {next_link}'
                f'</p>'
            )

        count_note = (
            f"<p class='muted' style='margin-bottom:12px'>{total} monster{'s' if total!=1 else ''}"
            f"{f' (showing {len(rows)})' if total > _MONSTERS_PER_PAGE else ''}</p>"
        )
        body = f"<h1>Monsters</h1>{search_box}{count_note}" + "".join(cards) + pager
        return render("Monsters", body)

    @app.route("/monsters/<slug>")
    def monster_detail(slug):
        if not _MONSTERS_DB.exists():
            abort(404)

        conn = db_get(_MONSTERS_DB)
        row = _resolve_by_slug(conn, slug)

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

        try:
            cats = json.loads(m["categories"]) if m.get("categories") else []
        except (ValueError, TypeError):
            cats = []
        meta_html = ""
        if cats:
            chips = "".join(
                f'<span style="display:inline-block;background:#2a2018;'
                f'border:1px solid #4a3828;color:#c8a96e;border-radius:10px;'
                f'padding:2px 10px;margin:0 6px 6px 0;font-size:0.8em">'
                f'{html.escape(c)}</span>'
                for c in cats
            )
            meta_html += f'<div style="margin-top:12px">{chips}</div>'
        if m.get("source"):
            meta_html += (
                f'<p style="margin-top:8px;font-size:0.82em;color:#8a7a60">'
                f'Source: {html.escape(str(m["source"]))}</p>'
            )

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

        portrait_path = monster_portrait_path(slug)
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
            f'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px">'
            f'<button id="gen-portrait-btn" '
            f'onclick="generateMonsterPortrait(\'{esc_slug}\')" '
            f'style="background:#3a2818;border:1px solid #5a4030;color:#c8a96e;'
            f'padding:6px 16px;border-radius:4px;cursor:pointer;font-size:0.9em;white-space:nowrap">'
            f'{btn_label}</button>'
            f'<input id="custom-portrait-prompt" type="text" '
            f'placeholder="Custom prompt — overrides auto-generated description" '
            f'style="background:#2a2018;border:1px solid #4a3828;color:#d4c5a9;'
            f'padding:5px 10px;border-radius:4px;font-size:0.85em;flex:1;min-width:200px">'
            f'</div>'
            f'<span id="gen-portrait-status" style="font-size:0.85em;color:#8a7a60"></span>'
            f'</div>'
            f'<script>'
            f'async function generateMonsterPortrait(slug){{'
            f'  const btn=document.getElementById("gen-portrait-btn");'
            f'  const status=document.getElementById("gen-portrait-status");'
            f'  const cp=document.getElementById("custom-portrait-prompt").value.trim();'
            f'  btn.disabled=true; btn.textContent="Generating…";'
            f'  try{{'
            f'    const opts={{method:"POST"}};'
            f'    if(cp){{opts.headers={{"Content-Type":"application/json"}};'
            f'         opts.body=JSON.stringify({{prompt:cp}});}}'
            f'    const r=await fetch("/monsters/"+slug+"/generate-portrait",opts);'
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
            + (f'<div class="card" style="margin-top:12px">{desc_html}{meta_html}</div>'
               if (desc_html or meta_html) else "")
        )
        return render(f"{html.escape(name)} — Monsters", body)
