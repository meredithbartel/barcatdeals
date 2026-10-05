/* Progressive enhancement: the server remains the source of deals and filters. */
(() => {
  'use strict';
  const root = document.body;
  const stage = document.querySelector('.map-stage');
  if (!stage) return;
  const $ = (selector) => document.querySelector(selector);
  const camera = window.BarDealsCamera;
  const theme = window.BarDealsTheme;
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const smallScreen = () => window.matchMedia('(max-width: 760px)').matches;
  const readData = (scope = document) => JSON.parse(scope.querySelector('#map-data').textContent);
  let data = readData();
  const catalogue = JSON.parse($('#catalogue-data').textContent);
  let localStorage = null;
  try { localStorage = window.localStorage; } catch (_) { /* Private browsers can refuse storage. */ }
  const night = window.BarDealsPlan.create(localStorage, catalogue);
  let browseMode = 'places';
  let directoryCards = [...document.querySelector('.place-directory').children].map(node => node.cloneNode(true));
  let toastTimer = null;
  let map = null;
  let mapLoaded = false;
  let is3D = true;
  let selected = null;
  let requestController = null;
  let loadingTimer = null;
  let dragMode = 'move';
  let overviewCamera = null;
  let last3DPitch = 50;
  let clusterPopup = null;
  let clusterKey = null;
  const markers = new Map();
  root.classList.add('js-ready');

  const animate = (duration) => reducedMotion.matches ? 0 : duration;
  const announce = (message) => { $('#explorer-announcement').textContent = message; };
  const coordinates = (venue) => [venue.longitude, venue.latitude];
  const isPlaced = (venue) => Number.isFinite(venue.latitude) && Number.isFinite(venue.longitude);
  const findVenue = (slug) => data.venues.find(venue => venue.slug === slug) || catalogue.find(venue => venue.slug === slug);
  const visiblePlaces = () => browseMode === 'saved' ? night.getSaved().map(findVenue).filter(Boolean) : data.venues;
  const browsablePlaces = () => visiblePlaces().some(venue => venue.slug === selected) ? visiblePlaces() : catalogue;
  const cameraState = () => ({center: map.getCenter(), zoom: map.getZoom(), pitch: map.getPitch(), bearing: map.getBearing(), padding: map.getPadding()});
  const setControlsDisabled = (disabled) => document.querySelectorAll('.map-toolbar button, .map-neighborhoods button, .camera-dock button, .camera-dock input').forEach((control) => { control.disabled = disabled; });

  // Below this zoom nearby places group; at or above it every pin is shown.
  const SPREAD_ZOOM = 15;
  // Rough on-screen footprint of a pin (pill + stem), plus a little air, so
  // spreading can keep prices from covering each other without measuring DOM.
  const pinSize = (venue) => venue.deal_count
    ? {width: Math.max(44, String(venue.cheapest === null ? 'Deal' : `$${venue.cheapest}`).length * 10 + 20) + 8, height: 46}
    : {width: 40, height: 44};

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  }

  function toast(message) {
    clearTimeout(toastTimer);
    $('#explorer-toast').textContent = message;
    $('#explorer-toast').hidden = false;
    toastTimer = setTimeout(() => { $('#explorer-toast').hidden = true; }, 3200);
  }

  function savedCard(venue, index) {
    const card = node('li', 'place-card'); card.dataset.place = venue.slug;
    const avatar = node('div', `place-avatar${venue.kind === 'restaurant' ? ' place-avatar--food' : ''}`);
    avatar.setAttribute('aria-hidden', 'true'); avatar.append(node('span', '', venue.name[0]), node('i', '', String(index + 1).padStart(2, '0')));
    const body = node('div', 'place-body');
    const name = node('a', 'place-name', venue.name); name.href = venue.venue_url; name.dataset.selectVenue = venue.slug;
    const bottom = node('div', 'place-bottom');
    const add = node('button', 'place-add enhancement-only', '+'); add.type = 'button'; add.dataset.planAdd = venue.slug; add.setAttribute('aria-label', `Add ${venue.name} to your night`);
    bottom.append(node('span', 'place-offer', 'Saved for another good night'), add);
    body.append(node('span', 'place-kind', venue.kind_label), name, node('p', 'place-address', venue.address || 'Address not confirmed'), bottom);
    const save = node('button', 'save-place enhancement-only', '♥'); save.type = 'button'; save.dataset.saveVenue = venue.slug; save.setAttribute('aria-label', `Save ${venue.name}`);
    card.append(avatar, body, save);
    return card;
  }

  function setBrowse(mode, updateURL = true) {
    browseMode = ['places', 'specials', 'saved'].includes(mode) ? mode : 'places';
    root.dataset.browse = browseMode;
    document.querySelectorAll('button[data-browse]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.browse === browseMode)));
    const saved = browseMode === 'saved';
    const places = visiblePlaces();
    $('.place-directory').replaceChildren(...(saved ? places.map(savedCard) : directoryCards.map(card => card.cloneNode(true))));
    $('#directory-title').textContent = saved ? 'Saved for later' : 'The neighborhood lineup';
    $('#directory-count').textContent = `${places.length} places`;
    $('#directory-note').textContent = saved ? 'Your favorites, ready whenever you are.' : 'Independent favorites and familiar stops.';
    $('#saved-empty').hidden = !saved || places.length > 0;
    $('#header-saved').setAttribute('aria-pressed', String(saved));
    if (updateURL) {
      const url = new URL(location.href);
      url.searchParams.set('panel', browseMode);
      history.replaceState({}, '', url);
    }
    syncPersonal();
    updateMarkers();
  }

  function syncPersonal() {
    const saved = new Set(night.getSaved());
    const stops = night.getStops();
    $('#saved-count').textContent = saved.size;
    $('#plan-count').textContent = stops.length;
    document.querySelectorAll('[data-save-venue]').forEach(button => {
      const active = saved.has(button.dataset.saveVenue);
      button.textContent = active ? '♥' : '♡'; button.setAttribute('aria-pressed', String(active));
    });
    document.querySelectorAll('[data-plan-add]').forEach(button => {
      const active = stops.some(venue => venue.slug === button.dataset.planAdd);
      button.classList.toggle('is-added', active); button.textContent = active ? '✓' : '+';
      button.disabled = active;
    });
    $('#drawer-save').setAttribute('aria-pressed', String(saved.has(selected)));
    $('#drawer-save').textContent = saved.has(selected) ? '♥' : '♡';
    const added = stops.some(venue => venue.slug === selected);
    $('#drawer-add-plan').textContent = added ? '✓ In your night plan' : '+ Add to my night';
    $('#drawer-add-plan').disabled = added;
    $('#plan-empty').hidden = stops.length > 0;
    $('#plan-footer').hidden = stops.length === 0;
    $('#plan-stops').replaceChildren(...stops.map((venue, index) => {
      const item = node('li', 'plan-stop');
      const body = node('div', 'stop-body');
      const title = node('button', 'stop-name', venue.name); title.type = 'button'; title.dataset.selectVenue = venue.slug;
      const controls = node('div', 'stop-controls');
      for (const [direction, label, symbol] of [[-1, 'Move earlier', '↑'], [1, 'Move later', '↓']]) {
        const button = node('button', '', symbol); button.type = 'button'; button.setAttribute('aria-label', `${label}: ${venue.name}`);
        button.disabled = direction < 0 ? index === 0 : index === stops.length - 1;
        button.addEventListener('click', () => { night.moveStop(venue.slug, direction); syncPersonal(); updateMarkers(); $('#plan-stops').children[index + direction]?.querySelector('.stop-name').focus(); });
        controls.append(button);
      }
      const remove = node('button', 'stop-remove', 'Remove'); remove.type = 'button'; remove.setAttribute('aria-label', `Remove ${venue.name} from your night`);
      remove.addEventListener('click', () => { night.removeStop(venue.slug); syncPersonal(); updateMarkers(); ($('#plan-stops').children[Math.min(index, night.getStops().length - 1)]?.querySelector('.stop-name') || $('#plan-explore')).focus(); });
      controls.append(remove);
      body.append(title, node('p', 'stop-address', venue.address || 'Address not confirmed'), controls);
      item.append(node('span', 'stop-number', index + 1), body);
      return item;
    }));
    const directions = night.directionsUrl();
    $('#plan-directions').hidden = !directions;
    if (directions) $('#plan-directions').href = directions;
    else $('#plan-directions').removeAttribute('href');
    $('#plan-route-note').textContent = directions ? 'Your stops open as a walking route in Google Maps.' :
      (stops.length < 2 ? 'Add another stop to get walking directions.' : 'Directions need a confirmed location for every stop.');
  }

  function addStop(slug) {
    const venue = findVenue(slug);
    if (!venue) return;
    if (night.addStop(slug)) toast(`${venue.name} added to your night.`);
    else if (night.getStops().some(entry => entry.slug === slug)) toast('Already in your night plan.');
    else toast('Your night has five stops. Remove one to make room.');
    syncPersonal(); updateMarkers();
  }

  function toggleSaved(slug) {
    const venue = findVenue(slug);
    if (!venue) return;
    toast(night.toggleSaved(slug) ? `${venue.name} saved for later.` : `${venue.name} removed from saved places.`);
    if (browseMode === 'saved') setBrowse('saved', false);
    else syncPersonal();
  }

  function showPlan(open, returnFocus = true) {
    closeCluster();
    root.classList.toggle('has-plan', open);
    $('#night-plan-panel').hidden = !open;
    $('#header-plan').setAttribute('aria-expanded', String(open));
    if (open) { syncPersonal(); $('#night-plan-panel').focus({preventScroll: true}); }
    else if (returnFocus) $('#header-plan').focus({preventScroll: true});
  }

  function updateFocus() {
    if (!mapLoaded) return;
    const venue = findVenue(selected);
    map.getSource('barcat-focus')?.setData({type: 'FeatureCollection', features: venue && isPlaced(venue) ?
      [{type: 'Feature', properties: {}, geometry: {type: 'Point', coordinates: coordinates(venue)}}] : []});
  }

  function closeCluster(returnFocus = false) {
    const key = clusterKey;
    clusterPopup?.remove();
    clusterPopup = null;
    clusterKey = null;
    if (returnFocus && key) theme.markerForFocus(markers, key)?.getElement().focus({preventScroll: true});
  }

  function openCluster(group, location, key) {
    closeCluster();
    const panel = document.createElement('section');
    panel.className = 'cluster-menu';
    panel.setAttribute('aria-label', `${group.venues.length} places in this area`);
    const title = document.createElement('h3');
    title.textContent = `${group.venues.length} places, right here`;
    const note = document.createElement('p');
    note.textContent = group.venues.some(venue => venue.has_samples) ? 'Choose a place · sample deals included' : 'Bars and restaurants around this block';
    const list = document.createElement('div');
    list.className = 'cluster-menu-list';
    group.venues.forEach(venue => {
      const button = document.createElement('button');
      button.type = 'button';
      const name = document.createElement('span');
      name.textContent = venue.name;
      const price = document.createElement('small');
      price.textContent = Number.isFinite(venue.cheapest) ? `from $${venue.cheapest}` : (venue.deal_count ? 'Specials' : venue.kind_label || 'View place');
      button.append(name, price);
      button.addEventListener('click', () => selectVenue(venue.slug));
      list.append(button);
    });
    const zoom = document.createElement('button');
    zoom.type = 'button';
    zoom.className = 'cluster-zoom';
    zoom.textContent = 'Explore this block ↗';
    zoom.addEventListener('click', () => {
      closeCluster();
      closeVenue(false, false);
      const bounds = new maplibregl.LngLatBounds();
      group.venues.forEach(venue => bounds.extend(coordinates(venue)));
      map.fitBounds(bounds, {padding: camera.padding(stage.clientWidth, stage.clientHeight),
        maxZoom: 18.5, pitch: map.getPitch(), bearing: map.getBearing(), duration: animate(700)});
      map.getCanvas().focus({preventScroll: true});
    });
    panel.append(title, note, list, zoom);
    clusterKey = key;
    clusterPopup = new maplibregl.Popup({className: 'bar-cluster-popup', maxWidth: '290px',
      offset: 22, closeOnMove: true, focusAfterOpen: true})
      .setLngLat(location).setDOMContent(panel).addTo(map);
    const popup = clusterPopup;
    popup.on('close', () => {
      if (clusterPopup === popup) { clusterPopup = null; clusterKey = null; }
      if (document.activeElement === document.body) theme.markerForFocus(markers, key)?.getElement().focus({preventScroll: true});
    });
    announce(`${group.venues.length} places. Choose one to see its details.`);
  }

  function syncCamera() {
    if (!mapLoaded) return;
    const pitch = Math.round(map.getPitch());
    $('#camera-tilt').value = pitch;
    $('#tilt-value').textContent = `${pitch}°`;
    $('#compass-needle').style.transform = `rotate(${-map.getBearing()}deg)`;
    const next3D = pitch > 0;
    if (next3D !== is3D) {
      is3D = next3D;
      if (map.getLayer('barcat-buildings')) map.setLayoutProperty('barcat-buildings', 'visibility', is3D ? 'visible' : 'none');
      if (map.getLayer('barcat-building-footprints')) map.setLayoutProperty('barcat-building-footprints', 'visibility', is3D ? 'none' : 'visible');
    }
    $('#toggle-3d').setAttribute('aria-pressed', String(is3D));
    $('#toggle-3d').innerHTML = `<span class="control-cube" aria-hidden="true">◇</span> ${is3D ? '3D view' : '2D view'}`;
  }

  function setDragMode(mode) {
    dragMode = mode;
    root.classList.toggle('orbit-mode', mode === 'orbit');
    $('#move-mode').setAttribute('aria-pressed', String(mode === 'move'));
    $('#orbit-mode').setAttribute('aria-pressed', String(mode === 'orbit'));
    if (map) {
      if (mode === 'orbit') { map.dragPan.disable(); map.dragRotate.disable(); }
      else { map.dragPan.enable(); map.dragRotate.enable(); }
    }
    $('#map-gesture-hint').textContent = smallScreen() ? 'Two fingers to move, pinch to zoom · use the controls to tilt' :
      (mode === 'orbit' ? 'Drag left/right to rotate · up/down to tilt' : 'Drag to move · right-drag to rotate · scroll to zoom');
  }

  function bindOrbitDrag() {
    const canvas = map.getCanvas();
    let start = null;
    canvas.addEventListener('pointerdown', (event) => {
      if (event.pointerType === 'touch' && dragMode === 'orbit') { setDragMode('move'); return; }
      if (dragMode !== 'orbit' || event.button !== 0 || event.pointerType === 'touch') return;
      event.preventDefault();
      map.stop();
      start = {x: event.clientX, y: event.clientY, pointer: event.pointerId, ...cameraState()};
      canvas.setPointerCapture(event.pointerId);
      canvas.classList.add('is-dragging');
    });
    canvas.addEventListener('pointermove', (event) => {
      if (!start || event.pointerId !== start.pointer) return;
      map.jumpTo(camera.orbit(start, event.clientX - start.x, event.clientY - start.y));
    });
    const release = () => { start = null; canvas.classList.remove('is-dragging'); };
    canvas.addEventListener('pointerup', release);
    canvas.addEventListener('pointercancel', release);
    canvas.addEventListener('lostpointercapture', release);
  }

  function failMap(message) {
    clearTimeout(loadingTimer);
    $('#map-loading').hidden = true;
    $('#map-fallback').hidden = false;
    if (message) $('#map-error-message').textContent = message;
  }

  function fitBars(area = null, duration = 850) {
    if (!mapLoaded) return;
    const places = camera.district(visiblePlaces(), area);
    if (!places.length) return;
    closeCluster();
    document.querySelectorAll('[data-map-area]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.mapArea === area)));
    const bounds = new maplibregl.LngLatBounds();
    places.forEach((venue) => bounds.extend(coordinates(venue)));
    map.fitBounds(bounds, {
      padding: camera.padding(stage.clientWidth, stage.clientHeight),
      maxZoom: area ? 17.3 : 16.5, pitch: is3D ? 50 : 0, bearing: area ? -12 : map.getBearing(),
      duration: animate(duration),
    });
  }

  // The basemap is generated in JS, so a CSS theme change cannot reach it.
  // Read the same source of truth theme.js writes.
  function themeMode() {
    const explicit = document.documentElement.getAttribute('data-theme');
    if (explicit === 'dark' || explicit === 'light') return explicit;
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches
      ? 'dark' : 'light';
  }

  // Recolour in place: tiles, the focus ring and the markers all stay put.
  document.addEventListener('bardeals:theme', () => {
    if (!map || !mapLoaded) return;
    try {
      theme.repaint(map, is3D, themeMode());
    } catch (error) {
      /* A failed restyle must never take the map down with it. */
    }
  });

  function initMap() {
    if (map || root.classList.contains('map-page--list')) return;
    $('#map-loading').hidden = false;
    $('#map-fallback').hidden = true;
    if (!window.maplibregl) {
      failMap('The map could not start. Try reloading, or browse every special in the list.');
      return;
    }
    try {
      map = new maplibregl.Map({
        container: 'neighborhood-map', style: theme.create(is3D, themeMode(), animate(450)),
        center: [-84.5148, 39.1289], zoom: 16.3, pitch: is3D ? last3DPitch : 0, bearing: -12,
        minZoom: 12, maxZoom: 19, maxPitch: 75,
        cooperativeGestures: smallScreen(),
        attributionControl: true, canvasContextAttributes: {antialias: true},
        pixelRatio: Math.min(2, Math.max(1.5, window.devicePixelRatio || 1)),
      });
      bindOrbitDrag();
      setDragMode(dragMode);
      map.on('pitch', syncCamera);
      map.on('rotate', syncCamera);
      map.on('moveend', updateMarkers);
      map.on('movestart', (event) => {
        if (event.originalEvent) document.querySelectorAll('[data-map-area]').forEach(button => button.setAttribute('aria-pressed', 'false'));
      });
      map.on('load', () => {
        clearTimeout(loadingTimer);
        mapLoaded = true;
        $('#map-loading').hidden = true;
        $('#map-fallback').hidden = true;
        setControlsDisabled(false);
        syncCamera();
        // Open at street scale so the buildings and places are immediately useful.
        // Filtered and saved searches still frame their own results.
        const params = new URL(location.href).searchParams;
        const focusedSearch = ['q', 'kind', 'max', 'cat'].some(key => params.get(key));
        fitBars(!focusedSearch && browseMode !== 'saved' && camera.district(visiblePlaces(), 'short-vine').length ? 'short-vine' : null, 0);
        updateMarkers();
        if (selected) selectVenue(selected, false);
      });
      // A missing tile should not discard a working map. A failed initial style should.
      map.on('error', () => {
        if (!mapLoaded && !navigator.onLine) failMap();
      });
      loadingTimer = setTimeout(() => {
        if (!mapLoaded) failMap('The map is taking too long to load. Check your connection or use the deal list while the tiles are unavailable.');
      }, 18000);
    } catch (error) {
      if (map) map.remove();
      map = null;
      failMap('3D graphics are unavailable in this browser. Try another browser, or explore the same specials in the list.');
    }
  }

  function updateMarkers() {
    const focused = document.activeElement?.closest('.bar-marker')?.dataset.markerKey;
    markers.forEach((marker) => marker.remove());
    markers.clear();
    $('#mapped-count').textContent = `${data.mapped_count} places mapped${data.unmapped_count ? ` · ${data.unmapped_count} location unconfirmed` : ''}`;
    document.querySelectorAll('[data-map-area]').forEach(button => { button.disabled = !mapLoaded || !camera.district(visiblePlaces(), button.dataset.mapArea).length; });
    if (!mapLoaded) return;
    const places = [...visiblePlaces()];
    const selection = findVenue(selected);
    if (selection && !places.some(venue => venue.slug === selected)) places.push(selection);
    const project = (point) => map.project(point);
    // Zoomed out, the whole district is a few hundred pixels across and
    // spreading would scatter pins far from their streets, so group instead.
    // Closer in, every place keeps its own pin and price.
    const groups = map.getZoom() < SPREAD_ZOOM
      ? theme.groupVenues(places.filter(isPlaced), project, selected, 88)
      : theme.spreadVenues(places.filter(isPlaced), project, selected, pinSize)
        .map(({venue, dx, dy}) => ({venues: [venue], dx, dy}));
    groups.forEach((group) => {
      const venue = group.venues[0];
      const clustered = group.venues.length > 1;
      const key = clustered ? `cluster:${group.venues.map((entry) => entry.slug).join(',')}` : venue.slug;
      const hasDeals = group.venues.some((entry) => entry.deal_count);
      const button = document.createElement('button');
      button.type = 'button';
      const restaurant = !clustered && venue.kind === 'restaurant';
      button.className = `bar-marker${hasDeals ? '' : ' bar-marker--quiet'}${clustered ? ' bar-marker--cluster' : ''}${restaurant && !hasDeals ? ' bar-marker--restaurant' : ''}`;
      button.dataset.markerKey = key;
      button.setAttribute('aria-label', clustered ? `Choose from ${group.venues.length} places: ${group.venues.map((entry) => entry.name).join(', ')}` :
        `${venue.name}, ${venue.deal_count} specials${venue.has_samples ? ', includes sample data' : ''}`);
      if (!clustered) button.setAttribute('aria-pressed', String(selected === venue.slug));
      const pill = document.createElement('span');
      pill.className = 'marker-pill';
      pill.textContent = clustered ? `${group.venues.length} places +` :
        (venue.deal_count ? (venue.cheapest === null ? 'Deal' : `$${venue.cheapest}`) : 'b.');
      if (restaurant && !hasDeals) pill.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3v5m3-5v5m3-5v5M5 7v2a3 3 0 0 0 6 0V7M8 12v9M19 3c-3 3-4 6-4 10h4m0-10v18"/></svg>';
      const label = document.createElement('span');
      label.className = 'marker-label';
      label.textContent = clustered ? 'Choose a place in this group' : venue.name;
      button.append(pill, label);
      const stopIndex = night.getStops().findIndex(entry => entry.slug === venue.slug);
      if (!clustered && stopIndex >= 0) button.append(node('span', 'marker-stop', stopIndex + 1));
      button.classList.toggle('is-active', !clustered && selected === venue.slug);
      button.addEventListener('click', (event) => {
        // The map's background click closes popups; this click opens one.
        event.stopPropagation();
        if (!clustered) { selectVenue(venue.slug); return; }
        openCluster(group, location, key);
      });
      const location = clustered ? [
        group.venues.reduce((sum, entry) => sum + entry.longitude, 0) / group.venues.length,
        group.venues.reduce((sum, entry) => sum + entry.latitude, 0) / group.venues.length,
      ] : coordinates(venue);
      const offset = [Math.round(group.dx || 0), Math.round(group.dy || 0)];
      const distance = Math.hypot(offset[0], offset[1]);
      if (distance > 6) {
        // A thin leader from the nudged pin back to where the place really is.
        button.classList.add('bar-marker--moved');
        const leader = node('span', 'marker-leader');
        leader.style.width = `${distance}px`;
        leader.style.transform = `rotate(${Math.atan2(-offset[1], -offset[0])}rad)`;
        const spot = node('span', 'marker-spot');
        spot.style.transform = `translate(${-offset[0]}px, ${-offset[1]}px)`;
        button.prepend(leader, spot);
      }
      markers.set(key, new maplibregl.Marker({element: button, anchor: 'bottom', offset})
        .setLngLat(location).addTo(map));
    });
    if (focused) {
      const replacement = theme.markerForFocus(markers, focused);
      replacement?.getElement().focus({preventScroll: true});
    }
  }

  function closeVenue(returnFocus = false, restoreCamera = true) {
    if (!selected) return;
    const previous = selected;
    selected = null;
    $('#venue-drawer').hidden = true;
    stage.classList.remove('has-selection');
    root.classList.remove('has-venue');
    document.querySelectorAll('.deal.is-selected').forEach((card) => card.classList.remove('is-selected'));
    markers.forEach((marker) => {
      marker.getElement().classList.remove('is-active');
      marker.getElement().setAttribute('aria-pressed', 'false');
    });
    if (restoreCamera && overviewCamera && mapLoaded) map.easeTo({...overviewCamera, duration: animate(650)});
    overviewCamera = null;
    updateFocus();
    updateMarkers();
    if (returnFocus) {
      const marker = theme.markerForFocus(markers, previous)?.getElement();
      if (marker) marker.focus({preventScroll: true});
      else $('#explorer-sidebar .head-title')?.scrollIntoView({block: 'nearest'});
    }
  }

  function selectVenue(slug, focus = true, moveCamera = true) {
    const venue = findVenue(slug);
    if (!venue) return;
    showPlan(false, false);
    closeCluster();
    if (!overviewCamera && mapLoaded) overviewCamera = cameraState();
    selected = slug;
    if (root.classList.contains('map-page--list')) setView('map');
    $('#drawer-name').textContent = venue.name;
    const neighbours = browsablePlaces();
    $('#venue-position').textContent = `${neighbours.findIndex((entry) => entry.slug === slug) + 1} of ${neighbours.length}`;
    $('#previous-venue').disabled = $('#next-venue').disabled = neighbours.length < 2;
    $('#drawer-address').textContent = venue.address || 'Street address not confirmed';
    $('#drawer-eyebrow').textContent = venue.deal_count ? `${venue.deal_count} SPECIAL${venue.deal_count === 1 ? '' : 'S'} ON THIS DAY` : (venue.kind_label || 'Explore this place').toUpperCase();
    $('#drawer-week').href = venue.venue_url;
    $('#drawer-website').hidden = !venue.website_url;
    if (venue.website_url) $('#drawer-website').href = venue.website_url;
    else $('#drawer-website').removeAttribute('href');
    $('#drawer-directions').hidden = !venue.directions_url;
    if (venue.directions_url) $('#drawer-directions').href = venue.directions_url;
    const notes = [];
    if (venue.has_samples) notes.push('Sample deals included — check current offers before you go.');
    if (!isPlaced(venue)) notes.push('Location not confirmed. This place has not been pinned on the map.');
    if (!data.venues.some(entry => entry.slug === slug)) notes.push('This place is outside your current filters. Open its weekly specials or website to see more.');
    else if (!venue.deal_count) notes.push(venue.website_url ? 'No confirmed specials for this day. Visit the official website for menus and current offers.' : 'No confirmed specials for this day. Check with the venue for current offers.');
    $('#drawer-note').textContent = notes.join(' ');
    $('#drawer-deals').replaceChildren();
    document.querySelectorAll('#explorer-sidebar .deal').forEach((card) => {
      const matches = card.dataset.venue === slug;
      card.classList.toggle('is-selected', matches);
      if (matches) {
        const copy = card.cloneNode(true);
        copy.classList.remove('is-selected');
        copy.querySelectorAll('.show-on-map').forEach((button) => button.remove());
        $('#drawer-deals').append(copy);
      }
    });
    markers.forEach((marker, key) => {
      marker.getElement().classList.toggle('is-active', key === slug);
      marker.getElement().setAttribute('aria-pressed', String(key === slug));
    });
    $('#venue-drawer').hidden = false;
    stage.classList.add('has-selection');
    root.classList.add('has-venue');
    updateFocus();
    syncPersonal();
    updateMarkers();
    if (moveCamera && mapLoaded && isPlaced(venue)) map.flyTo({
      ...camera.selection(venue, cameraState(), is3D), duration: animate(850),
    });
    if (focus) {
      if (smallScreen()) $('#venue-drawer').scrollIntoView({behavior: reducedMotion.matches ? 'instant' : 'smooth', block: 'start'});
      $('#venue-drawer').focus({preventScroll: true});
      announce(`${venue.name} selected. ${venue.deal_count} specials on this day.`);
    }
  }

  function setView(mode, updateHistory = true) {
    closeCluster();
    const list = mode === 'list';
    if (list && selected) closeVenue(false, false);
    root.classList.toggle('map-page--list', list);
    document.querySelectorAll('[data-view]').forEach((link) => {
      if (link.dataset.view === mode) link.setAttribute('aria-current', 'true');
      else link.removeAttribute('aria-current');
    });
    if (updateHistory) {
      const url = new URL(location.href);
      url.searchParams.set('view', mode);
      history.pushState({}, '', url);
      // Keep the chosen display mode on subsequent server filter links.
      document.querySelectorAll('#explorer-sidebar a[href]').forEach((link) => {
        const target = new URL(link.href);
        if (target.origin === location.origin && target.pathname === '/' && !link.dataset.view) {
          target.searchParams.set('view', mode); link.href = target;
        }
      });
    }
    if (!list) { initMap(); requestAnimationFrame(() => map?.resize()); }
  }

  async function navigate(url, push = true) {
    closeCluster();
    requestController?.abort();
    const controller = new AbortController();
    requestController = controller;
    const sidebar = $('#explorer-sidebar');
    sidebar.setAttribute('aria-busy', 'true');
    try {
      const response = await fetch(url, {signal: controller.signal});
      if (!response.ok) throw new Error('Could not load deals');
      const page = new DOMParser().parseFromString(await response.text(), 'text/html');
      if (controller.signal.aborted) return;
      const nextSidebar = page.querySelector('#explorer-sidebar');
      if (!nextSidebar) throw new Error('Missing deal list');
      const nextData = readData(page);
      sidebar.replaceWith(nextSidebar);
      directoryCards = [...$('.place-directory').children].map(card => card.cloneNode(true));
      data = nextData;
      document.title = page.title;
      if (push) history.pushState({}, '', url);
      setView(new URL(url, location.href).searchParams.get('view') === 'list' ? 'list' : 'map', false);
      const params = new URL(url, location.href).searchParams;
      setBrowse(params.get('panel') || (params.has('max') || params.has('cat') ? 'specials' : 'places'), false);
      updateMarkers();
      if (selected && data.venues.some((venue) => venue.slug === selected)) selectVenue(selected, false, false);
      else closeVenue(false, false);
      announce(`Showing ${data.venues.reduce((total, venue) => total + venue.deal_count, 0)} specials. ${data.mapped_count} places mapped.`);
    } catch (error) {
      if (error.name !== 'AbortError') {
        announce('The filters could not update. Please try again.');
        sidebar.removeAttribute('aria-busy');
      }
    }
  }

  document.addEventListener('click', (event) => {
    const save = event.target.closest('[data-save-venue]');
    if (save) { toggleSaved(save.dataset.saveVenue); return; }
    const add = event.target.closest('[data-plan-add]');
    if (add) { addStop(add.dataset.planAdd); return; }
    const browse = event.target.closest('button[data-browse]');
    if (browse) { closeVenue(false, false); setBrowse(browse.dataset.browse); return; }
    if (event.target.closest('[data-open-plan]')) { showPlan(true); return; }
    const locate = event.target.closest('[data-select-venue]');
    if (locate) {
      if (locate.tagName === 'A' && (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0)) return;
      event.preventDefault(); selectVenue(locate.dataset.selectVenue); return;
    }
    const link = event.target.closest('a[href]');
    if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0 || link.target === '_blank') return;
    if (link.dataset.view) {
      event.preventDefault(); setView(link.dataset.view); return;
    }
    const url = new URL(link.href);
    if (link.closest('.kind-switch')) url.searchParams.set('panel', 'places');
    else if (link.closest('.discovery, .sortbar')) url.searchParams.set('panel', 'specials');
    else if (!url.searchParams.has('panel')) url.searchParams.set('panel', browseMode);
    if (link.closest('#explorer-sidebar') && url.origin === location.origin && url.pathname === '/') {
      event.preventDefault(); navigate(url.href);
    }
  });
  document.addEventListener('submit', (event) => {
    if (!event.target.matches('#explorer-sidebar form.filters')) return;
    event.preventDefault();
    const url = new URL(location.href);
    url.search = new URLSearchParams(new FormData(event.target)).toString();
    url.searchParams.set('view', root.classList.contains('map-page--list') ? 'list' : 'map');
    navigate(url.href);
  });
  $('#close-venue').addEventListener('click', () => closeVenue(true));
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { if (!$('#night-plan-panel').hidden) showPlan(false); else if (clusterPopup) closeCluster(true); else closeVenue(true); }
    if (event.key === 'Tab' && smallScreen() && !$('#night-plan-panel').hidden) {
      const controls = [...$('#night-plan-panel').querySelectorAll('button:not(:disabled), a[href]')].filter(element => !element.hidden && !element.closest('[hidden]'));
      const first = controls[0], last = controls.at(-1);
      if (event.shiftKey && (document.activeElement === first || document.activeElement === $('#night-plan-panel'))) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  });
  $('#header-saved').addEventListener('click', () => { closeVenue(false, false); showPlan(false, false); setBrowse('saved'); $('#explorer-sidebar').scrollIntoView({block: 'start', behavior: 'smooth'}); });
  $('#header-plan').addEventListener('click', () => showPlan($('#night-plan-panel').hidden));
  $('#close-plan').addEventListener('click', () => showPlan(false));
  $('#plan-explore').addEventListener('click', () => { showPlan(false); closeVenue(false, false); setBrowse('places'); $('#place-search').focus(); });
  $('#clear-plan').addEventListener('click', () => { night.clearStops(); syncPersonal(); updateMarkers(); $('#plan-explore').focus(); });
  $('#drawer-save').addEventListener('click', () => toggleSaved(selected));
  $('#drawer-add-plan').addEventListener('click', () => addStop(selected));
  document.querySelectorAll('[data-map-area]').forEach(button => button.addEventListener('click', () => {
    closeVenue(false, false);
    fitBars(button.dataset.mapArea);
    announce(`Showing places around ${button.textContent.replace('↗', '').trim()}.`);
  }));
  $('#fit-bars').addEventListener('click', () => { closeVenue(false, false); fitBars(); });
  $('#rotate-map').addEventListener('click', () => map?.easeTo({bearing: map.getBearing() + 30, duration: animate(350)}));
  $('#rotate-left').addEventListener('click', () => map?.easeTo({bearing: map.getBearing() - 30, duration: animate(350)}));
  $('#reset-north').addEventListener('click', () => map?.easeTo({bearing: 0, duration: animate(450)}));
  $('#zoom-in').addEventListener('click', () => map?.zoomIn({duration: animate(300)}));
  $('#zoom-out').addEventListener('click', () => map?.zoomOut({duration: animate(300)}));
  $('#move-mode').addEventListener('click', () => setDragMode('move'));
  $('#orbit-mode').addEventListener('click', () => setDragMode('orbit'));
  $('#camera-tilt').addEventListener('input', (event) => {
    const pitch = Number(event.target.value);
    if (pitch > 0) last3DPitch = pitch;
    map?.jumpTo({pitch});
  });
  $('#previous-venue').addEventListener('click', () => selectVenue(camera.nextVenue(browsablePlaces(), selected, -1), false));
  $('#next-venue').addEventListener('click', () => selectVenue(camera.nextVenue(browsablePlaces(), selected, 1), false));
  $('#toggle-3d').addEventListener('click', () => {
    if (is3D) last3DPitch = map.getPitch();
    const targetPitch = is3D ? 0 : Math.max(last3DPitch, 50);
    map?.easeTo({pitch: targetPitch, duration: animate(550)});
  });
  $('#retry-map').addEventListener('click', () => {
    closeCluster();
    clearTimeout(loadingTimer); map?.remove(); map = null; mapLoaded = false;
    setControlsDisabled(true);
    initMap();
  });
  window.addEventListener('popstate', () => navigate(location.href, false));
  window.addEventListener('resize', () => { map?.resize(); if (smallScreen() && dragMode === 'orbit') setDragMode('move'); });
  const initialParams = new URL(location.href).searchParams;
  setBrowse(initialParams.get('panel') || (initialParams.has('max') || initialParams.has('cat') ? 'specials' : 'places'), false);
  initMap();
})();
