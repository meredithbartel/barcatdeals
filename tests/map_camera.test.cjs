const {test} = require('node:test');
const assert = require('node:assert/strict');
const camera = require('../src/bardeals/static/map-camera.js');

test('orbit drags preserve starting orientation and clamp steep/extreme tilt', () => {
  assert.deepEqual(camera.orbit({bearing: 20, pitch: 60}, 100, 40), {bearing: 55, pitch: 50});
  assert.equal(camera.orbit({bearing: 0, pitch: 60}, 0, -1000).pitch, 75);
  assert.equal(camera.orbit({bearing: 0, pitch: 60}, 0, 1000).pitch, 0);
});

test('overview retains usable map area on desktop, phones, and short landscape screens', () => {
  for (const [width, height] of [[900, 700], [390, 410], [320, 240], [240, 160]]) {
    const padding = camera.padding(width, height);
    assert.ok(width - padding.left - padding.right > width * .7);
    assert.ok(height - padding.top - padding.bottom >= height * .5);
    assert.ok(Object.values(padding).every(value => value >= 0));
  }
});

test('bar selection preserves user bearing and centers the marker without a drawer offset', () => {
  const venue = {longitude: -84.51, latitude: 39.13};
  const result = camera.selection(venue, {zoom: 17.5, pitch: 68, bearing: 115}, true);
  assert.equal(result.bearing, 115);
  assert.equal(result.pitch, 68);
  assert.equal(result.zoom, 17.5);
  assert.deepEqual(result.center, [-84.51, 39.13]);
  assert.deepEqual(result.padding, {top: 0, bottom: 0, left: 0, right: 0});
  assert.equal(result.offset, undefined);
});

test('flat-map selection stays flat and overview zoom transitions to useful building detail', () => {
  const result = camera.selection({longitude: 0, latitude: 0}, {zoom: 13, pitch: 0, bearing: -20}, false);
  assert.equal(result.pitch, 0);
  assert.equal(result.zoom, 17);
  assert.equal(result.bearing, -20);
});

test('previous and next wrap around, including singleton and empty lists', () => {
  const venues = [{slug: 'a'}, {slug: 'b'}, {slug: 'c'}];
  assert.equal(camera.nextVenue(venues, 'a', -1), 'c');
  assert.equal(camera.nextVenue(venues, 'c', 1), 'a');
  assert.equal(camera.nextVenue([{slug: 'a'}], 'a', 1), 'a');
  assert.equal(camera.nextVenue([], null, 1), null);
});

test('area shortcuts fit matching locations and safely skip incomplete coordinates', () => {
  const venues = [
    {slug: 'vine', longitude: -84.509, latitude: 39.130},
    {slug: 'calhoun', longitude: -84.520, latitude: 39.127},
    {slug: 'unplaced', longitude: null, latitude: null},
    {slug: 'elsewhere', longitude: -84.55, latitude: 39.2},
  ];
  assert.deepEqual(camera.district(venues, 'short-vine').map(v => v.slug), ['vine']);
  assert.deepEqual(camera.district(venues, 'calhoun').map(v => v.slug), ['calhoun']);
  assert.deepEqual(camera.district(venues, null).map(v => v.slug), ['vine', 'calhoun', 'elsewhere']);
  assert.deepEqual(camera.district(venues.filter(v => v.slug !== 'vine'), 'short-vine'), []);
});
