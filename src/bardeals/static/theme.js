/* Theme switching.
 *
 * Three states, not two: "light", "dark", and no stored choice at all - which
 * follows the operating system. A visitor who has never touched the toggle
 * should get whatever their phone is already doing at 9pm.
 *
 * The inline bootstrap in base.html sets data-theme before first paint so the
 * page never flashes the wrong palette. This file only handles the toggle and
 * telling the map its palette changed.
 */
(function () {
  'use strict';

  var KEY = 'bardeals-theme';
  var root = document.documentElement;

  function stored() {
    try {
      var value = localStorage.getItem(KEY);
      return value === 'light' || value === 'dark' ? value : null;
    } catch (error) {
      return null; // Private mode, or site data blocked. Follow the system.
    }
  }

  function systemPrefersDark() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
  }

  function active() {
    return root.getAttribute('data-theme') || (systemPrefersDark() ? 'dark' : 'light');
  }

  function apply(mode) {
    root.setAttribute('data-theme', mode);
    var button = document.getElementById('theme-toggle');
    if (button) button.setAttribute('aria-label', 'Switch to ' + (mode === 'dark' ? 'light' : 'dark') + ' theme');
    // The basemap is drawn in JS, so it has to be told separately.
    document.dispatchEvent(new CustomEvent('bardeals:theme', {detail: {mode: mode}}));
  }

  function toggle() {
    var next = active() === 'dark' ? 'light' : 'dark';
    try {
      localStorage.setItem(KEY, next);
    } catch (error) {
      /* Not persisting is survivable; the page still switches for this visit. */
    }
    apply(next);
  }

  function init() {
    var button = document.getElementById('theme-toggle');
    if (button) {
      button.hidden = false;
      button.addEventListener('click', toggle);
      button.setAttribute('aria-label', 'Switch to ' + (active() === 'dark' ? 'light' : 'dark') + ' theme');
    }

    // Follow the system only while the visitor has made no explicit choice.
    if (window.matchMedia) {
      var query = window.matchMedia('(prefers-color-scheme: dark)');
      var onChange = function () {
        if (stored()) return;
        root.removeAttribute('data-theme');
        document.dispatchEvent(new CustomEvent('bardeals:theme', {detail: {mode: active()}}));
      };
      if (query.addEventListener) query.addEventListener('change', onChange);
      else if (query.addListener) query.addListener(onChange);
    }
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();

  window.BarDealsThemeMode = {active: active, apply: apply, toggle: toggle};
})();
