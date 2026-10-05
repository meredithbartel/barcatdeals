const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const vm = require('node:vm');
const planner = require('../src/bardeals/static/night-plan.js');
const SAVED = 'bardeals.saved.v1';
const PLAN = 'bardeals.plan.v1';
const catalogue = Array.from({length: 8}, (_, i) => ({
  slug: `venue_${i}`, name: `Venue ${i}`, latitude: 39.12 + i / 1000, longitude: -84.52,
}));
function storage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value)};
}
const slugs = plan => plan.getStops().map(venue => venue.slug);

test('corrupt JSON and non-array storage cannot manufacture saved venues or stops', () => {
  for (const value of ['{', 'null', 'false', '42', '"venue_0"', '{"slug":"venue_0"}']) {
    const plan = planner.create(storage({[SAVED]: value, [PLAN]: value}), catalogue);
    assert.deepEqual(plan.getSaved(), []);
    assert.deepEqual(plan.getStops(), []);
  }
});

test('hydration drops unknowns and invalid types, deduplicates, and limits only stops', () => {
  const values = [null, 4, {}, ['venue_0'], 'missing', 'venue_0', 'venue_0',
    ...catalogue.slice(1).map(venue => venue.slug)];
  const plan = planner.create(storage({[SAVED]: JSON.stringify(values), [PLAN]: JSON.stringify(values)}), catalogue);
  assert.deepEqual(plan.getSaved(), catalogue.map(venue => venue.slug));
  assert.deepEqual(slugs(plan), catalogue.slice(0, 5).map(venue => venue.slug));
  assert.equal(planner.limit, 5);
});

test('unknown and duplicate stops are rejected and a removed stop frees capacity', () => {
  const plan = planner.create(storage(), catalogue);
  assert.equal(plan.addStop('missing'), false);
  assert.equal(plan.addStop({slug: 'venue_0'}), false);
  for (const venue of catalogue.slice(0, 5)) assert.equal(plan.addStop(venue.slug), true);
  assert.equal(plan.addStop('venue_0'), false);
  assert.equal(plan.addStop('venue_5'), false);
  assert.equal(plan.removeStop('missing'), false);
  assert.equal(plan.removeStop('venue_2'), true);
  assert.equal(plan.addStop('venue_5'), true);
  assert.deepEqual(slugs(plan), ['venue_0', 'venue_1', 'venue_3', 'venue_4', 'venue_5']);
});

test('reordering swaps adjacent stops without wrapping or accepting invalid directions', () => {
  const plan = planner.create(storage(), catalogue);
  ['venue_0', 'venue_1', 'venue_2'].forEach(slug => plan.addStop(slug));
  for (const [slug, direction] of [['venue_0', -1], ['venue_2', 1], ['missing', 1],
    ['venue_1', 0], ['venue_1', 2], ['venue_1', '1']]) {
    assert.equal(plan.moveStop(slug, direction), false);
  }
  assert.deepEqual(slugs(plan), ['venue_0', 'venue_1', 'venue_2']);
  assert.equal(plan.moveStop('venue_0', 1), true);
  assert.deepEqual(slugs(plan), ['venue_1', 'venue_0', 'venue_2']);
  assert.equal(plan.moveStop('venue_2', -1), true);
  assert.deepEqual(slugs(plan), ['venue_1', 'venue_2', 'venue_0']);
});

test('saved venues are independent of stops and survive a fresh instance', () => {
  const disk = storage();
  const plan = planner.create(disk, catalogue);
  assert.equal(plan.toggleSaved('missing'), false);
  assert.equal(plan.toggleSaved('venue_7'), true);
  assert.equal(plan.toggleSaved('venue_0'), true);
  assert.equal(plan.toggleSaved('venue_0'), false);
  ['venue_0', 'venue_1', 'venue_2'].forEach(slug => plan.addStop(slug));
  plan.moveStop('venue_0', 1);
  plan.removeStop('venue_2');
  const reopened = planner.create(disk, catalogue);
  assert.deepEqual(reopened.getSaved(), ['venue_7']);
  assert.deepEqual(slugs(reopened), ['venue_1', 'venue_0']);
  reopened.clearStops();
  assert.deepEqual(planner.create(disk, catalogue).getStops(), []);
  assert.deepEqual(planner.create(disk, catalogue).getSaved(), ['venue_7']);
});

test('missing storage and denied methods or property access preserve in-memory actions', () => {
  const fail = () => { throw new Error('Storage denied'); };
  for (const disk of [null, undefined, {}, {getItem: fail, setItem: fail},
    {get getItem() { return fail(); }, get setItem() { return fail(); }}]) {
    const plan = planner.create(disk, catalogue);
    assert.equal(plan.toggleSaved('venue_0'), true);
    assert.equal(plan.addStop('venue_0'), true);
    assert.equal(plan.addStop('venue_1'), true);
    assert.equal(plan.moveStop('venue_1', -1), true);
    assert.deepEqual(plan.getSaved(), ['venue_0']);
    assert.deepEqual(slugs(plan), ['venue_1', 'venue_0']);
  }
});

test('a quota error after successful hydration does not roll back the user action', () => {
  const disk = storage({[PLAN]: '["venue_0"]'});
  disk.setItem = () => { throw new Error('Quota exceeded'); };
  const plan = planner.create(disk, catalogue);
  assert.equal(plan.addStop('venue_1'), true);
  assert.deepEqual(slugs(plan), ['venue_0', 'venue_1']);
  plan.clearStops();
  assert.deepEqual(plan.getStops(), []);
});

test('getters and original catalogue cannot accidentally mutate plan identity or location', () => {
  const source = catalogue.map(venue => ({...venue}));
  const plan = planner.create(storage(), source);
  plan.addStop('venue_0');
  plan.toggleSaved('venue_0');
  plan.getSaved().push('venue_1');
  plan.getStops()[0].slug = 'missing';
  source[0].latitude = 90;
  assert.deepEqual(plan.getSaved(), ['venue_0']);
  assert.deepEqual(slugs(plan), ['venue_0']);
  assert.equal(plan.getStops()[0].latitude, 39.12);
});

test('walking directions preserve order, valid zero coordinates, and encoded addresses', () => {
  const plan = planner.create(storage(), [
    {slug: 'zero', latitude: 0, longitude: 0},
    {slug: 'address', name: 'Café & Pub', address: ' 213 W McMillan St '},
    {slug: 'end', latitude: 39.13, longitude: -84.51},
  ]);
  assert.equal(plan.directionsUrl(), null);
  plan.addStop('zero');
  assert.equal(plan.directionsUrl(), null);
  plan.addStop('address');
  plan.addStop('end');
  const url = new URL(plan.directionsUrl());
  assert.equal(url.origin, 'https://www.google.com');
  assert.equal(url.pathname, '/maps/dir/');
  assert.equal(url.searchParams.get('api'), '1');
  assert.equal(url.searchParams.get('travelmode'), 'walking');
  assert.equal(url.searchParams.get('origin'), '0,0');
  assert.equal(url.searchParams.get('destination'), '39.13,-84.51');
  assert.equal(url.searchParams.get('waypoints'), 'Café & Pub, 213 W McMillan St, Cincinnati, OH');
  assert.match(url.href, /Caf%C3%A9/);
  assert.match(url.href, /%26/);
  plan.moveStop('end', -1);
  const reordered = new URL(plan.directionsUrl()).searchParams;
  assert.equal(reordered.get('waypoints'), '39.13,-84.51');
  assert.equal(reordered.get('destination'), 'Café & Pub, 213 W McMillan St, Cincinnati, OH');
  plan.removeStop('end');
  assert.equal(new URL(plan.directionsUrl()).searchParams.has('waypoints'), false);
});

test('missing, partial, nonnumeric and out-of-range coordinates require an actual address', () => {
  for (const data of [{}, {latitude: 0}, {latitude: null, longitude: null},
    {latitude: '39.13', longitude: '-84.51'}, {latitude: NaN, longitude: Infinity},
    {latitude: 91, longitude: 0}, {latitude: 0, longitude: -181}]) {
    const stops = [{slug: 'start', latitude: 0, longitude: 0}, {slug: 'bad', ...data, address: '  '}];
    const plan = planner.create(storage(), stops);
    ['start', 'bad'].forEach(slug => plan.addStop(slug));
    assert.equal(plan.directionsUrl(), null);
    const fallback = planner.create(storage(), [stops[0], {...stops[1], address: '12 Main St', city: 'Dayton', state: 'OH'}]);
    ['start', 'bad'].forEach(slug => fallback.addStop(slug));
    assert.equal(new URL(fallback.directionsUrl()).searchParams.get('destination'), '12 Main St, Dayton, OH');
  }
});

test('all five stops fit a mobile directions link with three intermediate waypoints', () => {
  const plan = planner.create(storage(), catalogue);
  catalogue.slice(0, 5).forEach(venue => plan.addStop(venue.slug));
  const params = new URL(plan.directionsUrl()).searchParams;
  assert.deepEqual(params.get('waypoints').split('|'), catalogue.slice(1, 4).map(venue => `${venue.latitude},${venue.longitude}`));
  assert.equal(params.get('destination'), `${catalogue[4].latitude},${catalogue[4].longitude}`);
});

test('browser UMD exposes the same planner without a DOM or CommonJS', () => {
  const scope = {URLSearchParams};
  vm.runInNewContext(readFileSync(require.resolve('../src/bardeals/static/night-plan.js'), 'utf8'), scope);
  assert.equal(scope.BarDealsPlan.limit, 5);
  assert.equal(scope.BarDealsPlan.create(null, catalogue).addStop('venue_0'), true);
});
