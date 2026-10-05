const {test} = require('node:test');
const assert = require('node:assert/strict');
const theme = require('../src/bardeals/static/map-theme.js');
const venue = (slug, x, y) => ({slug, longitude: x, latitude: y});
const project = ([x, y]) => ({x, y});

test('ground and roads cannot cover 3D buildings, and labels remain above them', () => {
  const layers = theme.create().layers;
  const buildings = layers.findIndex(layer => layer.type === 'fill-extrusion');
  assert.ok(buildings > 0);
  assert.ok(layers.slice(0, buildings).every(layer => layer.type !== 'symbol'));
  assert.ok(layers.slice(buildings + 1).every(layer => layer.type === 'symbol'));
});

test('2D footprints and 3D buildings never draw over each other', () => {
  for (const is3D of [true, false]) {
    const style = theme.create(is3D);
    const footprints = style.layers.find(layer => layer.id === 'barcat-building-footprints');
    const buildings = style.layers.find(layer => layer.id === 'barcat-buildings');
    assert.equal(buildings.layout.visibility, is3D ? 'visible' : 'none');
    assert.equal(footprints.layout.visibility, is3D ? 'none' : 'visible');
  }
});

test('dense pins group while isolated bars keep their individual marker', () => {
  const venues = [venue('a', 0, 0), venue('b', 20, 10), venue('c', 500, 500)];
  const groups = theme.groupVenues(venues, project, null);
  assert.deepEqual(groups.map(group => group.venues.map(v => v.slug)), [['a', 'b'], ['c']]);
  assert.deepEqual([groups[0].x, groups[0].y], [10, 5]);
  assert.equal(venues.length, 3);
});

test('a selected bar stays individually accessible even inside a dense group', () => {
  const groups = theme.groupVenues([venue('a', 0, 0), venue('b', 1, 1), venue('c', 2, 2)], project, 'b');
  assert.deepEqual(groups.find(group => group.selected).venues.map(v => v.slug), ['b']);
  assert.deepEqual(groups.find(group => !group.selected).venues.map(v => v.slug), ['a', 'c']);
});

test('street-level zoom exposes all bars, including coincident coordinates', () => {
  const groups = theme.groupVenues([venue('a', 0, 0), venue('b', 0, 0)], project, null, 0);
  assert.equal(groups.length, 2);
  assert.ok(groups.every(group => group.venues.length === 1));
});

test('groups use projected screen positions and do not change with deal sort order', () => {
  const venues = [venue('c', 100, 100), venue('a', 0, 0), venue('b', 20, 10)];
  const zoomed = ([x, y]) => ({x: x * 10, y: y * 10});
  assert.equal(theme.groupVenues(venues, zoomed, null).length, 3);
  assert.deepEqual(theme.groupVenues(venues, project, null), theme.groupVenues([...venues].reverse(), project, null));
  assert.deepEqual(theme.groupVenues([], project, null), []);
});

test('groups merge when later additions move their centroids within the collision radius', () => {
  const venues = [venue('a', 0, 0), venue('b', 100, 0), venue('c', 60, 0), venue('d', 60, 0)];
  const groups = theme.groupVenues(venues, project, null, 64);
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].venues.map(v => v.slug), ['a', 'b', 'c', 'd']);
  assert.deepEqual([groups[0].x, groups[0].y], [55, 0]);
  assert.deepEqual(theme.groupVenues([...venues].reverse(), project, null, 64), groups);
});

test('centroid merging leaves selected pins separate and remaining groups apart', () => {
  const venues = [venue('a', 0, 0), venue('b', 100, 0), venue('c', 60, 0),
    venue('d', 60, 0), venue('selected', 55, 0), venue('z', 500, 0)];
  const groups = theme.groupVenues(venues, project, 'selected', 64);
  assert.deepEqual(groups.find(group => group.selected).venues.map(v => v.slug), ['selected']);
  const clustered = groups.filter(group => !group.selected);
  for (let i = 0; i < clustered.length; i++) {
    for (let j = i + 1; j < clustered.length; j++) {
      assert.ok(Math.hypot(clustered[i].x - clustered[j].x, clustered[i].y - clustered[j].y) >= 64);
    }
  }
  assert.equal(theme.groupVenues(venues, project, 'selected', 0).length, venues.length);
});

test('marker focus survives individual-to-cluster and cluster-to-individual changes', () => {
  const a = {}, b = {}, cluster = {};
  assert.equal(theme.markerForFocus(new Map([['cluster:a,b', cluster]]), 'a'), cluster);
  assert.equal(theme.markerForFocus(new Map([['b', b], ['a', a]]), 'cluster:a,b'), a);
  assert.equal(theme.markerForFocus(new Map([['b', b]]), 'cluster:a,b'), b);
});

test('marker focus preserves exact matches and follows regrouped members without substring matches', () => {
  const exact = {}, related = {}, unrelated = {};
  const markers = new Map([['cluster:b,c', related], ['cluster:a,b', exact], ['cluster:bar2,c', unrelated]]);
  assert.equal(theme.markerForFocus(markers, 'cluster:a,b'), exact);
  assert.equal(theme.markerForFocus(new Map([['cluster:b,c', related]]), 'cluster:a,b'), related);
  assert.equal(theme.markerForFocus(new Map([['cluster:bar2,c', unrelated]]), 'bar'), null);
  assert.equal(theme.markerForFocus(markers, 'missing'), null);
  assert.equal(theme.markerForFocus(markers, null), null);
  assert.equal(theme.markerForFocus(new Map(), 'a'), null);
});

test('both palettes define the same roles, so neither theme falls back to a missing colour', () => {
  assert.deepEqual(Object.keys(theme.PALETTES.dark).sort(), Object.keys(theme.PALETTES.light).sort());
});

test('a theme change recolours the live map in place instead of reloading the style', () => {
  const live = theme.create(true, 'light');
  const calls = {paint: [], light: null, sky: null};
  const map = {
    getLayer: id => live.layers.find(layer => layer.id === id),
    setPaintProperty: (id, key, value) => calls.paint.push([id, key, value]),
    setLight: light => { calls.light = light; },
    setSky: sky => { calls.sky = sky; },
    setStyle: () => assert.fail('setStyle refetches every tile and blanks the canvas'),
  };
  theme.repaint(map, true, 'dark');
  const dark = theme.PALETTES.dark;
  assert.ok(calls.paint.some(([id, key, value]) => id === 'background' && key === 'background-color' && value === dark.background));
  assert.ok(calls.paint.some(([id, key, value]) => id === 'barcat-buildings' && key === 'fill-extrusion-color' && value === dark.building));
  assert.equal(calls.light.color, dark.lightColor);
  assert.equal(calls.sky['sky-color'], dark.sky);
});

test('theme colour changes fade unless motion is reduced', () => {
  assert.ok(theme.create(true, 'dark').transition.duration > 0);
  assert.equal(theme.create(true, 'dark', 0).transition.duration, 0);
});

const overlaps = (pins, size = {width: 56, height: 42}) => pins.some((a, i) => pins.slice(i + 1).some(b =>
  Math.abs((a.x + a.dx) - (b.x + b.dx)) < size.width - 1 && Math.abs((a.y + a.dy) - (b.y + b.dy)) < size.height - 1));

test('spreading keeps every place as its own pin and leaves none covering another', () => {
  const venues = [venue('a', 0, 0), venue('b', 10, 5), venue('c', 20, 0), venue('d', 0, 0), venue('far', 900, 900)];
  const pins = theme.spreadVenues(venues, project, null);
  assert.equal(pins.length, venues.length);
  assert.ok(!overlaps(pins));
  assert.deepEqual(pins.find(pin => pin.venue.slug === 'far'), {venue: venues[4], x: 900, y: 900, dx: 0, dy: 0});
});

test('spreading reports true positions, so leaders point back to the real spot', () => {
  const pins = theme.spreadVenues([venue('a', 100, 100), venue('b', 100, 100)], project, null);
  for (const pin of pins) assert.deepEqual([pin.x, pin.y], [100, 100]);
  assert.ok(pins.every(pin => pin.dy === 0 && Math.abs(pin.dx) >= 27), 'stacked pins sit side by side');
});

test('the selected pin stays on its spot and its neighbours make room', () => {
  const pins = theme.spreadVenues([venue('a', 0, 0), venue('b', 5, 0), venue('c', 0, 5)], project, 'b');
  const chosen = pins.find(pin => pin.venue.slug === 'b');
  assert.deepEqual([chosen.dx, chosen.dy], [0, 0]);
  assert.ok(!overlaps(pins));
});

test('spreading is deterministic regardless of input order', () => {
  const venues = [venue('c', 3, 1), venue('a', 0, 0), venue('b', 1, 2), venue('d', 2, 2)];
  const bySlug = pins => pins.map(pin => [pin.venue.slug, pin.dx, pin.dy]);
  assert.deepEqual(bySlug(theme.spreadVenues(venues, project, null)), bySlug(theme.spreadVenues([...venues].reverse(), project, null)));
});
