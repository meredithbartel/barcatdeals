/* Local favorites and an ordered evening plan; no DOM or network access. */
(function (scope, factory) {
  'use strict';
  if (typeof module === 'object' && module.exports) module.exports = factory();
  else if (typeof define === 'function' && define.amd) define([], factory);
  else scope.BarDealsPlan = factory();
})(globalThis, function () {
  'use strict';
  // Five stops leave three intermediate waypoints, including in mobile browsers.
  const LIMIT = 5;
  const SAVED_KEY = 'bardeals.saved.v1';
  const PLAN_KEY = 'bardeals.plan.v1';
  const text = value => typeof value === 'string' ? value.trim() : '';

  function location(venue) {
    const {latitude, longitude} = venue;
    if (Number.isFinite(latitude) && Math.abs(latitude) <= 90 &&
        Number.isFinite(longitude) && Math.abs(longitude) <= 180) {
      return `${latitude},${longitude}`;
    }
    if (!text(venue.address)) return null;
    return [text(venue.name), text(venue.address), text(venue.city) || 'Cincinnati',
      text(venue.state) || 'OH'].filter(Boolean).join(', ');
  }

  function create(storage, catalogue) {
    const venues = new Map();
    for (const venue of Array.isArray(catalogue) ? catalogue : []) {
      if (venue && typeof venue.slug === 'string' && text(venue.slug) === venue.slug &&
          venue.slug && !venues.has(venue.slug)) venues.set(venue.slug, {...venue});
    }
    function read(key, maximum) {
      try {
        const value = JSON.parse(storage.getItem(key));
        if (!Array.isArray(value)) return [];
        return [...new Set(value.filter(slug => typeof slug === 'string' && venues.has(slug)))].slice(0, maximum);
      } catch (_) { return []; }
    }
    function write(key, value) {
      try { storage.setItem(key, JSON.stringify(value)); }
      catch (_) { /* Private browsing or a full store still permits this session's plan. */ }
    }
    let saved = read(SAVED_KEY, venues.size);
    let stops = read(PLAN_KEY, LIMIT);
    function persistStops() { write(PLAN_KEY, stops); }

    return {
      getSaved() { return saved.slice(); },
      toggleSaved(slug) {
        if (!venues.has(slug)) return false;
        const present = saved.includes(slug);
        saved = present ? saved.filter(value => value !== slug) : [...saved, slug];
        write(SAVED_KEY, saved);
        return !present;
      },
      getStops() { return stops.map(slug => ({...venues.get(slug)})); },
      addStop(slug) {
        if (!venues.has(slug) || stops.includes(slug) || stops.length >= LIMIT) return false;
        stops.push(slug);
        persistStops();
        return true;
      },
      removeStop(slug) {
        const index = stops.indexOf(slug);
        if (index < 0) return false;
        stops.splice(index, 1);
        persistStops();
        return true;
      },
      moveStop(slug, direction) {
        if (direction !== -1 && direction !== 1) return false;
        const index = stops.indexOf(slug);
        const target = index + direction;
        if (index < 0 || target < 0 || target >= stops.length) return false;
        [stops[index], stops[target]] = [stops[target], stops[index]];
        persistStops();
        return true;
      },
      clearStops() { stops = []; persistStops(); },
      directionsUrl() {
        if (stops.length < 2) return null;
        const places = stops.map(slug => location(venues.get(slug)));
        // Never silently omit a stop whose location is unconfirmed.
        if (places.some(place => place === null)) return null;
        const query = new URLSearchParams({api: '1', origin: places[0],
          destination: places[places.length - 1], travelmode: 'walking'});
        if (places.length > 2) query.set('waypoints', places.slice(1, -1).join('|'));
        return `https://www.google.com/maps/dir/?${query}`;
      },
    };
  }
  return {limit: LIMIT, create};
});
