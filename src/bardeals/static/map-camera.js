/* Camera geometry is shared by the controls and covered without a GPU. */
(function (scope) {
  'use strict';
  const clamp = (value, low, high) => Math.min(high, Math.max(low, value));
  const camera = {
    // Bounds follow campus and Gaslight corridors; unplaced venues stay in the list.
    district(venues, key) {
      const bounds = {'short-vine': [-84.512, 39.126, -84.5065, 39.135],
        calhoun: [-84.523, 39.122, -84.515, 39.1305],
        gaslight: [-84.524, 39.138, -84.511, 39.147]}[key];
      return venues.filter(venue => Number.isFinite(venue.longitude) && Number.isFinite(venue.latitude) &&
        (!bounds || (venue.longitude >= bounds[0] && venue.latitude >= bounds[1] &&
          venue.longitude <= bounds[2] && venue.latitude <= bounds[3])));
    },
    orbit(start, dx, dy) {
      return {bearing: start.bearing + dx * 0.35, pitch: clamp(start.pitch - dy * 0.25, 0, 75)};
    },
    // Leave space for the compact header and camera dock, even on small screens.
    padding(width, height) {
      return {top: Math.min(165, height * 0.29), bottom: Math.min(130, height * 0.2),
        left: Math.min(55, width * 0.12), right: Math.min(55, width * 0.12)};
    },
    nextVenue(venues, selected, direction) {
      if (!venues.length) return null;
      const index = venues.findIndex((venue) => venue.slug === selected);
      return venues[(Math.max(index, 0) + direction + venues.length) % venues.length].slug;
    },
    // Bar details live beside/below the map, so there is no drawer offset to guess.
    selection(venue, current, is3D) {
      return {center: [venue.longitude, venue.latitude],
        zoom: clamp(Math.max(current.zoom, 17), 12, 19),
        pitch: is3D ? Math.max(current.pitch, 50) : 0,
        bearing: current.bearing, padding: {top: 0, bottom: 0, left: 0, right: 0}};
    },
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = camera;
  else scope.BarDealsCamera = camera;
})(globalThis);
