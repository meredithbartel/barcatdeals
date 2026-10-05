/* Live countdowns.
 *
 * The page is rendered once and only meta-refreshes hourly, so a server-printed
 * "ends in 33m" can be most of an hour wrong by the time it is read - and a
 * happy hour that closed twenty minutes ago is exactly the thing this app
 * exists to get right.
 *
 * Progressive enhancement: the server label is already correct at render time,
 * so with JS off nothing is lost. This only keeps it honest as the minutes pass,
 * and moves a card into "finished" once its window actually closes.
 */
(function () {
  'use strict';

  var TICK_MS = 30000;
  var badges = [];
  var timer = null;

  function humanize(minutes) {
    if (minutes < 1) return 'less than a minute';
    if (minutes < 60) return minutes + 'm';
    var hours = Math.floor(minutes / 60);
    var rest = minutes % 60;
    return rest === 0 ? hours + 'h' : hours + 'h ' + rest + 'm';
  }

  function collect() {
    badges = [];
    var nodes = document.querySelectorAll('[data-ends-at], [data-starts-at]');
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var ends = node.getAttribute('data-ends-at');
      var starts = node.getAttribute('data-starts-at');
      var at = Date.parse(ends || starts || '');
      if (isNaN(at)) continue;
      badges.push({
        node: node,
        at: at,
        kind: ends ? 'ends' : 'starts',
        card: node.closest ? node.closest('.deal') : null,
        original: node.textContent,
      });
    }
    return badges.length;
  }

  function render() {
    var now = Date.now();
    var stillLive = false;

    for (var i = 0; i < badges.length; i++) {
      var badge = badges[i];
      var minutes = Math.round((badge.at - now) / 60000);

      if (minutes > 0) {
        stillLive = true;
        // Only take over the wording inside the horizon the server uses, so a
        // deal running until 2am still reads "until 02:00" rather than "6h".
        if (badge.kind === 'ends' && minutes <= 90) {
          badge.node.textContent = 'ends in ' + humanize(minutes);
        } else if (badge.kind === 'starts') {
          badge.node.textContent = 'starts in ' + humanize(minutes);
        }
        continue;
      }

      // The moment has passed while the page sat open.
      if (badge.kind === 'ends') {
        badge.node.textContent = 'just finished';
        badge.node.classList.remove('badge--now');
        badge.node.classList.add('badge--over');
        if (badge.card) {
          badge.card.classList.remove('deal--live');
          badge.card.classList.add('deal--over', 'deal--just-ended');
        }
      } else {
        badge.node.textContent = 'starting now';
        badge.node.classList.remove('badge--later');
        badge.node.classList.add('badge--now');
        if (badge.card) badge.card.classList.add('deal--live');
      }
      badges.splice(i, 1);
      i--;
    }

    if (!stillLive && !badges.length && timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  function start() {
    if (timer) clearInterval(timer);
    if (!collect()) return;
    render();
    timer = setInterval(render, TICK_MS);
  }

  // A phone that has been asleep wakes with stale text; refresh on return.
  document.addEventListener('visibilitychange', function () {
    if (!document.hidden) start();
  });

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();

  window.BarDealsCountdown = {refresh: start};
})();
