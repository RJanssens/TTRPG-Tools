/**
 * CombatMap — canvas tactical map renderer for the TTRPG dashboard.
 *
 * Usage:
 *   const map = new CombatMap(canvasEl, { cellPx: 36 });
 *   map.onHover = (x, y) => { ... };   // optional: called on mousemove
 *   map.onClick = (x, y) => { ... };   // optional: called on click
 *   map.render(state);                 // state = /combat-state JSON
 *   map.renderInitList(containerEl, combatants);  // sidebar initiative order
 */
class CombatMap {
    constructor(canvas, options) {
        options = options || {};
        this.canvas = canvas;
        this.ctx    = canvas.getContext('2d');
        this.cellPx = options.cellPx || 36;
        this._imgs  = {};   // portrait URL → Image cache

        var self = this;
        canvas.addEventListener('mousemove', function(e) {
            var r = canvas.getBoundingClientRect();
            var x = Math.floor((e.clientX - r.left)  / self.cellPx);
            var y = Math.floor((e.clientY - r.top)   / self.cellPx);
            if (self.onHover) self.onHover(x, y);
        });
        canvas.addEventListener('click', function(e) {
            var r = canvas.getBoundingClientRect();
            var x = Math.floor((e.clientX - r.left)  / self.cellPx);
            var y = Math.floor((e.clientY - r.top)   / self.cellPx);
            if (self.onClick) self.onClick(x, y);
        });
    }

    // ── Main render entry point ───────────────────────────────────────────────

    render(state) {
        if (!state || !state.grid) return;
        var g = state.grid;
        var C = this.cellPx;

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
            ctx.beginPath();
            ctx.moveTo(x * C, 0);
            ctx.lineTo(x * C, g.height * C);
            ctx.stroke();
        }
        for (var y = 0; y <= g.height; y++) {
            ctx.beginPath();
            ctx.moveTo(0, y * C);
            ctx.lineTo(g.width * C, y * C);
            ctx.stroke();
        }
    }

    _drawTerrain(g, C) {
        var ctx    = this.ctx;
        var cells  = g.cells  || {};
        var styles = g.styles || {};

        for (var key in cells) {
            var type   = cells[key];
            var parts  = key.split(',');
            var cx     = parseInt(parts[0], 10);
            var cy     = parseInt(parts[1], 10);
            var st     = styles[type] || { color: '#555', symbol: '?' };

            ctx.fillStyle = st.color;
            ctx.fillRect(cx * C + 1, cy * C + 1, C - 2, C - 2);

            ctx.fillStyle      = 'rgba(255,255,255,0.75)';
            ctx.font           = Math.floor(C * 0.52) + 'px monospace';
            ctx.textAlign      = 'center';
            ctx.textBaseline   = 'middle';
            ctx.fillText(st.symbol, cx * C + C / 2, cy * C + C / 2);
        }
    }

    _drawTokens(combatants, C) {
        for (var i = 0; i < combatants.length; i++) {
            var c = combatants[i];
            if (c.x === null || c.x === undefined) continue;
            if (c.y === null || c.y === undefined) continue;
            this._drawToken(c, c.x * C + C / 2, c.y * C + C / 2, C);
        }
    }

    _drawToken(c, px, py, C) {
        var ctx = this.ctx;
        var r   = C * 0.38;

        // Outer ring: gold if current turn, otherwise HP colour
        ctx.beginPath();
        ctx.arc(px, py, r + 3, 0, Math.PI * 2);
        ctx.strokeStyle = c.current ? '#c8a96e' : this._hpColor(c.hp, c.hp_max);
        ctx.lineWidth   = c.current ? 2.5 : 1.5;
        ctx.stroke();

        // Clip to circle, draw portrait or monogram fallback
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
                var dw    = img.naturalWidth  * scale;
                var dh    = img.naturalHeight * scale;
                ctx.drawImage(img, px - dw / 2, py - dh / 2, dw, dh);
                drawn = true;
            }
        }
        if (!drawn) {
            ctx.fillStyle = c.side === 'party' ? '#2a4a2a' : '#4a2a2a';
            ctx.fillRect(px - r, py - r, r * 2, r * 2);
            ctx.fillStyle      = '#d4c5a9';
            ctx.font           = 'bold ' + Math.floor(r * 0.9) + 'px sans-serif';
            ctx.textAlign      = 'center';
            ctx.textBaseline   = 'middle';
            ctx.fillText((c.name || '?')[0].toUpperCase(), px, py);
        }
        ctx.restore();

        // Name label below token
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

        // Sort: lowest init first (fastest), unrolled after
        var sorted = combatants.slice().sort(function(a, b) {
            if (a.init === null && b.init === null) return 0;
            if (a.init === null) return  1;
            if (b.init === null) return -1;
            return a.init - b.init;
        });

        var html = '';
        for (var i = 0; i < sorted.length; i++) {
            var c        = sorted[i];
            var dead     = c.hp !== undefined && c.hp <= -10;
            var side     = c.side === 'party' ? 'party' : 'enemy';
            var curCls   = c.current ? ' init-current' : '';
            var initStr  = (c.init !== null && c.init !== undefined) ? c.init : '—';
            var opacity  = dead ? ' style="opacity:0.35"' : '';
            var hpColor  = this._hpColor(c.hp, c.hp_max);
            var initial  = (c.name || '?')[0].toUpperCase();

            var portrait;
            if (c.portrait_url) {
                portrait = '<img src="' + c.portrait_url + '" alt="' + _esc(c.name) + '"'
                         + ' onerror="this.parentNode.innerHTML=\'<span>' + initial + '</span>\'">';
            } else {
                portrait = '<span>' + initial + '</span>';
            }

            html += '<div class="init-row ' + side + curCls + '"' + opacity + '>'
                  + '<div class="init-portrait" style="border-color:' + hpColor + '">' + portrait + '</div>'
                  + '<div class="init-info">'
                  + '<span class="init-name">' + _esc(c.name) + '</span>'
                  + '<span class="init-val">' + initStr + '</span>'
                  + '</div>'
                  + '</div>';
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
        if (!this._imgs[url]) {
            var img = new Image();
            img.src = url;
            this._imgs[url] = img;
        }
        return this._imgs[url];
    }
}

function _esc(s) {
    return (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
