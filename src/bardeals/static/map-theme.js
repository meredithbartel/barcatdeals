/* A quiet architectural basemap. Ground first, buildings next, labels last. */
(function (scope) {
  'use strict';
  const source = {source: 'openmaptiles'};
  const names = ['coalesce', ['get', 'name_en'], ['get', 'name']];
  const classes = (...values) => ['in', ['get', 'class'], ['literal', values]];
  const width = (near, far) => ['interpolate', ['linear'], ['zoom'], 12, near, 19, far];
  const fill = (id, layer, color, filter) => ({id, type: 'fill', ...source,
    'source-layer': layer, ...(filter ? {filter} : {}), paint: {'fill-color': color}});
  const road = (id, filter, color, near, far, minzoom = 12) => ({
    id, type: 'line', ...source, 'source-layer': 'transportation', minzoom,
    filter: ['all', ['==', ['geometry-type'], 'LineString'], filter],
    layout: {'line-cap': 'round', 'line-join': 'round'},
    paint: {'line-color': color, 'line-width': width(near, far)},
  });
  const streetLabel = (id, filter, minzoom, size, c) => ({
    id, type: 'symbol', ...source, 'source-layer': 'transportation_name', minzoom, filter,
    layout: {'symbol-placement': 'line', 'symbol-spacing': 450,
      'text-field': names, 'text-font': ['Noto Sans Regular'], 'text-size': size,
      'text-max-angle': 25, 'text-padding': 12},
    paint: {'text-color': c.streetText, 'text-halo-color': c.streetHalo, 'text-halo-width': 2},
  });

  // Two readings of the same map: daylight paper, and the city after dark.
  // Keyed by role rather than colour so both stay in step when either is tuned.
  const PALETTES = {
    light: {
      background: '#e6ebe3', residential: '#e0e6df', campus: '#e9e9dc',
      parks: '#d2e0c8', woodland: '#ceddc7', grass: '#d8e4ce', water: '#b7d6d9',
      paths: '#f5f7ef', minorCasing: '#cfd7ce', majorCasing: '#c8d1c8',
      minorRoad: '#f8faf5', majorRoad: '#fffdf7',
      footprint: '#c5cfc5', footprintLine: '#b1bfb3', building: '#e0e6dc',
      focusFill: '#d58450', focusLine: '#b85d39',
      streetText: '#56665e', streetHalo: '#f8faf5',
      placeText: '#798775', placeHalo: '#edf1e7',
      lightColor: '#fff9ec', lightIntensity: 0.28,
      sky: '#e6eeec', horizon: '#f4f6f0',
    },
    // Night: near-neutral charcoal ground so the pins carry the colour, lit
    // buildings that stand clear of the street, and roads kept quieter than
    // the blocks they run between - with a faint sodium warmth on the main ones.
    dark: {
      background: '#131512', residential: '#151714', campus: '#171915',
      parks: '#141c16', woodland: '#131b15', grass: '#151d16', water: '#0e1b22',
      paths: '#1f221d', minorCasing: '#0e100d', majorCasing: '#0e100d',
      minorRoad: '#252823', majorRoad: '#3b3529',
      footprint: '#272a26', footprintLine: '#373a35', building: '#3a3d39',
      focusFill: '#e8815a', focusLine: '#f0966f',
      streetText: '#b5b2a3', streetHalo: '#131512',
      placeText: '#9a9b8e', placeHalo: '#131512',
      lightColor: '#fff2dc', lightIntensity: 0.42,
      sky: '#131512', horizon: '#1b1d19',
    },
  };

  // `fade` is how long colour changes take when the theme flips; 0 under reduced motion.
  function create(is3D = true, mode = 'light', fade = 450) {
    const c = PALETTES[mode] || PALETTES.light;
    return {
      version: 8, name: 'Clifton / architectural',
      transition: {duration: fade, delay: 0},
      glyphs: 'https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf',
      sources: {openmaptiles: {type: 'vector', url: 'https://tiles.openfreemap.org/planet'},
        'barcat-focus': {type: 'geojson', data: {type: 'FeatureCollection', features: []}}},
      light: {anchor: 'viewport', color: c.lightColor, intensity: c.lightIntensity, position: [1.5, 210, 35]},
      sky: {'sky-color': c.sky, 'horizon-color': c.horizon,
        'sky-horizon-blend': 0.8, 'atmosphere-blend': 0},
      layers: [
        {id: 'background', type: 'background', paint: {'background-color': c.background}},
        fill('residential', 'landuse', c.residential, classes('residential')),
        fill('campus', 'landuse', c.campus, classes('college', 'university', 'school')),
        fill('parks', 'landuse', c.parks, classes('park', 'recreation_ground', 'cemetery')),
        fill('woodland', 'landcover', c.woodland, classes('wood')),
        fill('grass', 'landcover', c.grass, classes('grass')),
        fill('water', 'water', c.water),
        {id: 'streams', type: 'line', ...source, 'source-layer': 'waterway',
          paint: {'line-color': c.water, 'line-width': width(1, 5)}},
        road('paths', classes('path', 'track'), c.paths, 0.25, 3, 15),
        road('minor-road-casing', classes('minor', 'service'), c.minorCasing, 0.6, 19),
        road('major-road-casing', classes('primary', 'secondary', 'tertiary', 'trunk', 'motorway'), c.majorCasing, 2, 30),
        road('minor-roads', classes('minor', 'service'), c.minorRoad, 0.3, 17),
        road('major-roads', classes('primary', 'secondary', 'tertiary', 'trunk', 'motorway'), c.majorRoad, 1, 27),
        {...fill('barcat-building-footprints', 'building', c.footprint), minzoom: 14,
          layout: {visibility: is3D ? 'none' : 'visible'},
          paint: {'fill-color': c.footprint, 'fill-outline-color': c.footprintLine}},
        {id: 'barcat-focus-ring', type: 'circle', source: 'barcat-focus',
          paint: {'circle-radius': ['interpolate', ['exponential', 2], ['zoom'], 12, 3, 19, 90],
            'circle-color': c.focusFill, 'circle-opacity': 0.22,
            'circle-stroke-color': c.focusLine, 'circle-stroke-width': 2, 'circle-stroke-opacity': 0.7,
            'circle-pitch-alignment': 'map', 'circle-pitch-scale': 'map'}},
        {id: 'barcat-buildings', type: 'fill-extrusion', ...source,
          'source-layer': 'building', minzoom: 14,
          layout: {visibility: is3D ? 'visible' : 'none'},
          filter: ['!=', ['get', 'hide_3d'], true],
          paint: {
            'fill-extrusion-color': c.building,
            'fill-extrusion-height': ['max', 3, ['coalesce', ['get', 'render_height'], 8]],
            'fill-extrusion-base': ['coalesce', ['get', 'render_min_height'], 0],
            'fill-extrusion-vertical-gradient': false,
            'fill-extrusion-opacity': 1,
          }},
        streetLabel('major-street-names', classes('primary', 'secondary', 'tertiary', 'trunk'), 14, 12, c),
        streetLabel('local-street-names', classes('minor'), 17.5, 11, c),
        {id: 'neighborhood-names', type: 'symbol', ...source, 'source-layer': 'place',
          minzoom: 12, maxzoom: 16.5, filter: classes('neighbourhood', 'suburb', 'quarter'),
          layout: {'text-field': names, 'text-font': ['Noto Sans Regular'],
            'text-size': 13, 'text-letter-spacing': 0.12, 'text-transform': 'uppercase', 'text-padding': 35},
          paint: {'text-color': c.placeText, 'text-halo-color': c.placeHalo, 'text-halo-width': 2}},
      ],
    };
  }

  // Recolour a live map in place. setStyle() would drop every loaded tile and
  // blank the canvas; the palettes differ only in paint, light and sky, so
  // push just those and let the style transition fade between them.
  function repaint(map, is3D, mode) {
    const next = create(is3D, mode);
    for (const layer of next.layers) {
      if (!map.getLayer(layer.id)) continue;
      for (const [key, value] of Object.entries(layer.paint || {})) map.setPaintProperty(layer.id, key, value);
    }
    map.setLight(next.light);
    if (map.setSky) map.setSky(next.sky);
  }

  // Group by actual screen distance, so rotated/tilted maps also avoid pin piles.
  // A selected venue always has its own marker. At street level, expose every pin.
  function groupVenues(venues, project, selected, radius = 64) {
    const groups = [];
    for (const venue of [...venues].sort((a, b) => a.slug.localeCompare(b.slug))) {
      const point = project([venue.longitude, venue.latitude]);
      const group = venue.slug !== selected && radius > 0 && groups.find((entry) =>
        !entry.selected && Math.hypot(point.x - entry.x, point.y - entry.y) < radius);
      if (group) {
        const count = group.venues.length;
        group.x = (group.x * count + point.x) / (count + 1);
        group.y = (group.y * count + point.y) / (count + 1);
        group.venues.push(venue);
      } else groups.push({x: point.x, y: point.y, selected: venue.slug === selected, venues: [venue]});
    }
    // Adding a venue moves its group's centroid, which can bring two existing
    // groups back into collision. Merge until every unselected pair is apart.
    if (radius > 0) {
      let merged = true;
      while (merged) {
        merged = false;
        for (let i = 0; i < groups.length && !merged; i++) {
          if (groups[i].selected) continue;
          for (let j = i + 1; j < groups.length; j++) {
            const left = groups[i], right = groups[j];
            if (right.selected || Math.hypot(left.x - right.x, left.y - right.y) >= radius) continue;
            const leftCount = left.venues.length, rightCount = right.venues.length;
            left.x = (left.x * leftCount + right.x * rightCount) / (leftCount + rightCount);
            left.y = (left.y * leftCount + right.y * rightCount) / (leftCount + rightCount);
            left.venues = [...left.venues, ...right.venues].sort((a, b) => a.slug.localeCompare(b.slug));
            groups.splice(j, 1);
            merged = true;
            break;
          }
        }
      }
    }
    return groups;
  }

  // Keep every pin, but nudge overlapping ones apart on screen just far enough
  // to read. Each result carries the pixel offset from its true position, so
  // the map can draw a leader back to the real spot. The selected pin never
  // moves; its neighbours make room for it. Deterministic for a given view.
  function spreadVenues(venues, project, selected, sizeOf = () => ({width: 56, height: 42})) {
    const pins = [...venues].sort((a, b) => a.slug.localeCompare(b.slug)).map((venue) => {
      const point = project([venue.longitude, venue.latitude]);
      return {venue, x: point.x, y: point.y, ox: point.x, oy: point.y,
        fixed: venue.slug === selected, ...sizeOf(venue)};
    });
    for (let pass = 0; pass < 80; pass++) {
      let moved = false;
      for (let i = 0; i < pins.length; i++) {
        for (let j = i + 1; j < pins.length; j++) {
          const a = pins[i], b = pins[j];
          if (a.fixed && b.fixed) continue;
          const dx = b.x - a.x, dy = b.y - a.y;
          const overlapX = (a.width + b.width) / 2 - Math.abs(dx);
          const overlapY = (a.height + b.height) / 2 - Math.abs(dy);
          if (overlapX <= 0.5 || overlapY <= 0.5) continue;
          moved = true;
          // Push along the shallower overlap; side by side when exactly stacked.
          const sideways = overlapX < overlapY || (dx === 0 && dy === 0);
          const push = sideways ? overlapX : overlapY;
          const sign = (sideways ? dx : dy) < 0 ? -1 : 1;
          const share = a.fixed ? [0, 1] : b.fixed ? [1, 0] : [0.5, 0.5];
          if (sideways) { a.x -= sign * push * share[0]; b.x += sign * push * share[1]; }
          else { a.y -= sign * push * share[0]; b.y += sign * push * share[1]; }
        }
      }
      if (!moved) break;
    }
    return pins.map(({venue, x, y, ox, oy}) => ({venue, x: ox, y: oy, dx: x - ox, dy: y - oy}));
  }

  function markerForFocus(markers, previousKey) {
    if (typeof previousKey !== 'string' || !previousKey) return null;
    if (markers.has(previousKey)) return markers.get(previousKey);
    const members = previousKey.startsWith('cluster:') ? previousKey.slice(8).split(',') : [previousKey];
    // When a group splits, prefer its first surviving individual bar.
    for (const slug of members) if (markers.has(slug)) return markers.get(slug);
    const previousMembers = new Set(members);
    for (const [key, marker] of markers) {
      if (key.startsWith('cluster:') && key.slice(8).split(',').some(slug => previousMembers.has(slug))) return marker;
    }
    return null;
  }

  const theme = {create, repaint, groupVenues, spreadVenues, markerForFocus, PALETTES};
  if (typeof module !== 'undefined' && module.exports) module.exports = theme;
  else scope.BarDealsTheme = theme;
})(globalThis);
