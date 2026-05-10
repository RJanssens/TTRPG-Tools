/**
 * World map renderer — Leaflet w/ Simple CRS, consumes the GeoJSON `_style`
 * attribute set by tools/world_map.py:_resolve_styles.
 *
 * Wiring (set before this script loads):
 *   window.WORLD_MAP_SLUG = "sheldomar";
 *   window.WORLD_MAP_VIEW = "world";
 *   window.WORLD_MAP_DATA = "/api/maps/sheldomar/world.geojson";
 *   window.WORLD_MAP_PARTY = "/api/maps/sheldomar/party.geojson";  // 404 OK
 */
(function () {
    var dataUrl   = window.WORLD_MAP_DATA;
    var partyUrl  = window.WORLD_MAP_PARTY;
    var container = document.getElementById('worldmap');
    var sidebar   = document.getElementById('worldmap-sidebar');

    if (!container || !dataUrl) return;

    var map = L.map(container, {
        crs: L.CRS.Simple,
        minZoom: -6,
        maxZoom: 6,
        zoomSnap: 0.25,
        attributionControl: false,
    });

    // Author writes coords as [x, y] with y increasing UP. Leaflet's Simple CRS
    // is [y, x] with y increasing DOWN. Negate y so the visual matches author intent.
    function coordsToLatLng(c) { return L.latLng(-c[1], c[0]); }

    function escapeHTML(s) {
        return String(s).replace(/[<>&"]/g, function (c) {
            return ({ '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;' })[c];
        });
    }

    function styleForFeature(feature) {
        var s = (feature.properties && feature.properties._style) || {};
        return {
            color:       s.stroke || s.color || '#888',
            weight:      s['stroke-width'] || 1,
            fillColor:   s.color || '#888',
            fillOpacity: s['fill-opacity'] || 0.5,
            opacity:     1,
        };
    }

    function pointToLayer(feature, latlng) {
        var s = (feature.properties && feature.properties._style) || {};
        return L.circleMarker(latlng, {
            radius:      s['marker-size'] || 6,
            color:       s.stroke || '#000',
            fillColor:   s.color || '#888',
            weight:      s['stroke-width'] || 1,
            fillOpacity: 1,
        });
    }

    function describe(p) {
        var lines = [];
        if (p.id)          lines.push('<b>' + escapeHTML(p.id) + '</b>');
        if (p.kind)        lines.push('<i>' + escapeHTML(p.kind) + '</i>');
        if (p.description) lines.push(escapeHTML(p.description));
        if (p.tags && p.tags.length) {
            lines.push('Tags: ' + p.tags.map(escapeHTML).join(', '));
        }
        // Show numeric/string properties not already covered.
        var skip = { id: 1, kind: 1, description: 1, tags: 1, _style: 1, doc: 1, in: 1 };
        Object.keys(p).forEach(function (k) {
            if (skip[k]) return;
            var v = p[k];
            if (v == null || typeof v === 'object') return;
            lines.push(escapeHTML(k) + ': ' + escapeHTML(String(v)));
        });
        if (p.in)  lines.push('In: ' + escapeHTML(p.in));
        if (p.doc) {
            // Strip extension and use last path component as a slug into /locations.
            var slug = String(p.doc).split('/').pop().replace(/\.md$/, '');
            lines.push('<a href="/locations/_/' + escapeHTML(slug) + '">→ doc</a>');
        }
        return lines.join('<br>');
    }

    function showSidebar(p) {
        if (!sidebar) return;
        sidebar.innerHTML = describe(p);
    }

    function onEachFeature(feature, layer) {
        var p = feature.properties || {};
        var html = describe(p);
        layer.bindPopup(html);
        layer.on('click', function () { showSidebar(p); });
    }

    // ── Layer management: group features by `kind` so users can toggle ─────
    var layersByKind = {};
    function addToKindLayer(kind, layer) {
        if (!layersByKind[kind]) {
            layersByKind[kind] = L.layerGroup().addTo(map);
        }
        layersByKind[kind].addLayer(layer);
    }

    // The layers control is created once, after the main features load.
    // The party overlay gets added to it later (if/when its fetch resolves)
    // so users can uncheck it like any other layer.
    var layersControl = null;
    var partyLayer    = null;

    fetch(dataUrl)
        .then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        })
        .then(function (geo) {
            if (geo.error) {
                container.innerHTML = '<p style="color:#c66;padding:16px">'
                    + 'Compile error: ' + escapeHTML(geo.error) + '</p>';
                return;
            }
            // Build features per-feature so we can place each into a kind layer.
            var allBounds = null;
            (geo.features || []).forEach(function (feat) {
                var sub = L.geoJSON({
                    type: 'FeatureCollection',
                    features: [feat],
                }, {
                    style:           styleForFeature,
                    pointToLayer:    pointToLayer,
                    onEachFeature:   onEachFeature,
                    coordsToLatLng:  coordsToLatLng,
                });
                addToKindLayer(feat.properties.kind || 'other', sub);
                try {
                    var b = sub.getBounds();
                    if (b.isValid()) {
                        allBounds = allBounds ? allBounds.extend(b) : b;
                    }
                } catch (e) { /* point-only or empty */ }
            });

            // Layer toggle control (one entry per kind). Saved so the party
            // overlay can register itself once it lands.
            var overlays = {};
            Object.keys(layersByKind).forEach(function (k) {
                overlays[k] = layersByKind[k];
            });
            layersControl = L.control.layers(null, overlays, { collapsed: false, position: 'topright' }).addTo(map);
            // If the party overlay arrived before us, register it now.
            if (partyLayer) layersControl.addOverlay(partyLayer, 'party');

            if (allBounds && allBounds.isValid()) {
                map.fitBounds(allBounds.pad(0.1));
            } else {
                map.setView([0, 0], 0);
            }

            // Display view metadata (units, scope) in the sidebar header if present.
            if (sidebar && geo.metadata) {
                var meta = geo.metadata;
                sidebar.innerHTML =
                    '<div style="color:#8a7a60;font-size:.85em;margin-bottom:8px">'
                    + 'Scope: <b>' + escapeHTML(meta.scope || '') + '</b>'
                    + (meta.units ? ' · Units: <b>' + escapeHTML(meta.units) + '</b>' : '')
                    + '</div>'
                    + '<div style="color:#8a7a60;font-style:italic">Click a feature for details.</div>';
            }
        })
        .catch(function (err) {
            container.innerHTML = '<p style="color:#c66;padding:16px">'
                + 'Failed to load map: ' + escapeHTML(String(err.message || err)) + '</p>';
        });

    // ── Party overlay — toggleable via the layers control ───────────────────
    if (partyUrl) {
        fetch(partyUrl).then(function (r) {
            if (!r.ok) return null;
            return r.json();
        }).then(function (geo) {
            if (!geo) return;
            partyLayer = L.geoJSON(geo, {
                pointToLayer: function (feature, latlng) {
                    var p = feature.properties || {};
                    var label = escapeHTML(p.label || 'Party');
                    // Custom HTML icon so we can pulse around it via CSS.
                    var icon = L.divIcon({
                        className: 'party-marker',
                        html: '<span class="party-pulse"></span>'
                            + '<span class="party-dot"></span>'
                            + '<span class="party-label">' + label + '</span>',
                        iconSize:  [16, 16],
                        iconAnchor:[8, 8],
                    });
                    return L.marker(latlng, { icon: icon, interactive: false });
                },
                coordsToLatLng: coordsToLatLng,
            });
            partyLayer.addTo(map);
            // Register on the layers control so users can uncheck it.
            // If the control hasn't been created yet (main fetch slower),
            // the .then() above will register us instead.
            if (layersControl) layersControl.addOverlay(partyLayer, 'party');
        }).catch(function () { /* overlay missing is fine */ });
    }
})();
