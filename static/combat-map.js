/**
 * CombatMap — canvas tactical map renderer.
 *
 *   const map = new CombatMap(canvasEl, { cellPx: 36 });
 *   map.onHover = (x, y) => { ... };
 *   map.onClick = (x, y) => { ... };
 *   map.onZoom  = (z)    => { ... };   // fired after every zoom change
 *   map.render(state);                 // state from /combat-state JSON
 *   map.renderInitList(containerEl, combatants);
 */
class CombatMap {
    constructor(canvas, options) {
        options      = options || {};
        this.canvas  = canvas;
        this.ctx     = canvas.getContext('2d');
        this.cellPx  = options.cellPx || 36;
        this.zoom    = 1.0;
        this._imgs   = {};
        this._last   = null;

        var self = this;

        canvas.addEventListener('mousemove', function(e) {
            var r = canvas.getBoundingClientRect();
            var C = self._C();
            if (self.onHover) self.onHover(
                Math.floor((e.clientX - r.left) / C),
                Math.floor((e.clientY - r.top)  / C)
            );
        });

        canvas.addEventListener('click', function(e) {
            var r = canvas.getBoundingClientRect();
            var C = self._C();
            if (self.onClick) self.onClick(
                Math.floor((e.clientX - r.left) / C),
                Math.floor((e.clientY - r.top)  / C)
            );
        });

        canvas.addEventListener('wheel', function(e) {
            e.preventDefault();
            self.setZoom(self.zoom * (e.deltaY < 0 ? 1.15 : 0.87));
        }, { passive: false });
    }

    // ── Public API ────────────────────────────────────────────────────────────

    _C() { return Math.max(8, Math.round(this.cellPx * this.zoom)); }

    setZoom(z) {
        this.zoom = Math.max(0.25, Math.min(5.0, z));
        if (this._last) this.render(this._last);
        if (this.onZoom) this.onZoom(this.zoom);
    }

    render(state) {
        if (!state || !state.grid) return;
        this._last = state;
        var g = state.grid;
        var C = this._C();

        this.canvas.width  = g.width  * C;
        this.canvas.height = g.height * C;

        this._drawBackground(g, C);
        this._drawTerrain(g, C);
        this._drawTokens(state.combatants || [], C);
    }

    // ── Canvas layers ─────────────────────────────────────────────────────────

    _drawBackground(g, C) {
        var ctx = this.ctx;
        ctx.fillStyle = '#1a1510';
        ctx.fillRect(0, 0, g.width * C, g.height * C);
        ctx.strokeStyle = '#2e261e';
        ctx.lineWidth   = 0.5;
        for (var x = 0; x <= g.width; x++) {
            ctx.beginPath(); ctx.moveTo(x*C, 0); ctx.lineTo(x*C, g.height*C); ctx.stroke();
        }
        for (var y = 0; y <= g.height; y++) {
            ctx.beginPath(); ctx.moveTo(0, y*C); ctx.lineTo(g.width*C, y*C); ctx.stroke();
        }
    }

    _drawTerrain(g, C) {
        var ctx    = this.ctx;
        var cells  = g.cells  || {};
        var styles = g.styles || {};

        for (var key in cells) {
            var type = cells[key];
            var p    = key.split(',');
            var cx   = parseInt(p[0], 10);
            var cy   = parseInt(p[1], 10);
            var st   = styles[type] || { color: '#555', symbol: '?' };
            var px   = cx * C + 1;
            var py   = cy * C + 1;
            var pw   = C - 2;

            // Texture (scaled to fit cell) or solid colour
            var texImg   = st.texture ? this._getImg(st.texture) : null;
            var hasTexture = texImg && texImg.complete && texImg.naturalWidth > 0;

            if (hasTexture) {
                ctx.drawImage(texImg, px, py, pw, pw);
                // Subtle colour tint for thematic coherence
                ctx.fillStyle = st.color + '28';
                ctx.fillRect(px, py, pw, pw);
            } else {
                ctx.fillStyle = st.color;
                ctx.fillRect(px, py, pw, pw);
            }

            // Symbol: hidden on textured walls; semi-transparent on other textured cells
            var showSym = !hasTexture || type !== 'WALL';
            if (showSym) {
                ctx.fillStyle    = hasTexture ? 'rgba(255,255,255,0.45)' : 'rgba(255,255,255,0.75)';
                ctx.font         = Math.floor(C * 0.52) + 'px monospace';
                ctx.textAlign    = 'center';
                ctx.textBaseline = 'middle';
                ctx.fillText(st.symbol, cx*C + C/2, cy*C + C/2);
            }
        }
    }

    _drawTokens(combatants, C) {
        for (var i = 0; i < combatants.length; i++) {
            var c = combatants[i];
            if (c.x == null || c.y == null) continue;
            this._drawToken(c, c.x*C + C/2, c.y*C + C/2, C);
        }
    }

    _drawToken(c, px, py, C) {
        var ctx = this.ctx;
        var r   = C * 0.38;

        // Ring: gold = current turn; red = enemy; HP-colour = party
        ctx.beginPath();
        ctx.arc(px, py, r + 3, 0, Math.PI * 2);
        if (c.current) {
            ctx.strokeStyle = '#c8a96e'; ctx.lineWidth = 2.5;
        } else if (c.side !== 'party') {
            ctx.strokeStyle = '#8c2a2a'; ctx.lineWidth = 1.5;
        } else {
            ctx.strokeStyle = this._hpColor(c.hp, c.hp_max); ctx.lineWidth = 1.5;
        }
        ctx.stroke();

        // Clipped portrait or monogram fallback
        ctx.save();
        ctx.beginPath();
        ctx.arc(px, py, r, 0, Math.PI * 2);
        ctx.clip();

        var drawn = false;
        if (c.portrait_url) {
            var img = this._getImg(c.portrait_url);
            if (img.complete && img.naturalWidth > 0) {
                var s     = r * 2;
                var scale = Math.max(s / img.naturalWidth, s / img.naturalHeight);
                ctx.drawImage(img,
                    px - img.naturalWidth  * scale / 2,
                    py - img.naturalHeight * scale / 2,
                    img.naturalWidth  * scale,
                    img.naturalHeight * scale);
                drawn = true;
            }
        }
        if (!drawn) {
            ctx.fillStyle    = c.side === 'party' ? '#2a4a2a' : '#4a2a2a';
            ctx.fillRect(px - r, py - r, r*2, r*2);
            ctx.fillStyle    = '#d4c5a9';
            ctx.font         = 'bold ' + Math.floor(r * 0.9) + 'px sans-serif';
            ctx.textAlign    = 'center';
            ctx.textBaseline = 'middle';
            ctx.fillText((c.name || '?')[0].toUpperCase(), px, py);
        }
        ctx.restore();

        // Name label
        var label = c.name.length > 9 ? c.name.slice(0, 8) + '…' : c.name;
        ctx.shadowColor  = '#000';
        ctx.shadowBlur   = 3;
        ctx.fillStyle    = c.current ? '#c8a96e' : (c.side === 'party' ? '#8dc88d' : '#c88d8d');
        ctx.font         = Math.max(9, Math.floor(C * 0.26)) + 'px sans-serif';
        ctx.textAlign    = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(label, px, py + r + 2);
        ctx.shadowBlur   = 0;
    }

    // ── Initiative order sidebar (HTML) ───────────────────────────────────────

    renderInitList(containerEl, combatants) {
        if (!containerEl || !combatants) return;
        var sorted = combatants.slice().sort(function(a, b) {
            if (a.init == null && b.init == null) return 0;
            if (a.init == null) return  1;
            if (b.init == null) return -1;
            return a.init - b.init;
        });
        var html = '';
        for (var i = 0; i < sorted.length; i++) {
            var c       = sorted[i];
            var dead    = c.hp !== undefined && c.hp <= -10;
            var side    = c.side === 'party' ? 'party' : 'enemy';
            var curCls  = c.current ? ' init-current' : '';
            var initStr = (c.init != null) ? c.init : '—';
            var opacity = dead ? ' style="opacity:.35"' : '';
            var hpColor = this._hpColor(c.hp, c.hp_max);
            var initial = (c.name || '?')[0].toUpperCase();
            var portrait = c.portrait_url
                ? '<img src="' + c.portrait_url + '" alt="' + _esc(c.name) + '"'
                  + ' onerror="this.parentNode.innerHTML=\'<span>' + initial + '</span>\'">'
                : '<span>' + initial + '</span>';
            html += '<div class="init-row ' + side + curCls + '"' + opacity + '>'
                  + '<div class="init-portrait" style="border-color:' + hpColor + '">' + portrait + '</div>'
                  + '<div class="init-info">'
                  + '<span class="init-name">' + _esc(c.name) + '</span>'
                  + '<span class="init-val">' + initStr + '</span>'
                  + '</div></div>';
        }
        containerEl.innerHTML = html;
    }

    // ── Helpers ───────────────────────────────────────────────────────────────

    _hpColor(hp, hp_max) {
        if (!hp_max || hp <= 0) return '#555555';
        var r = hp / hp_max;
        if (r > 0.60) return '#4a8c4a';
        if (r > 0.25) return '#8c7a2a';
        return '#8c2a2a';
    }

    _getImg(url) {
        if (!this._imgs[url]) { var img = new Image(); img.src = url; this._imgs[url] = img; }
        return this._imgs[url];
    }
}

function _esc(s) {
    return (s || '')
        .replace(/&/g, '&amp;').replace(/</g, '&lt;')
        .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
