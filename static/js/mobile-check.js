/*
 * Blocks any screen under the desktop threshold, and any obvious
 * mobile/tablet user agent even if the window happens to be wide
 * (e.g. a phone in landscape, or dev-tools device emulation).
 * Re-checks on resize so rotating a tablet or shrinking a window
 * triggers it too.
 */
(function () {
  var MIN_WIDTH = 1024;
  var MIN_HEIGHT = 640;
  var MOBILE_UA = /Android|iPhone|iPad|iPod|Mobile|Windows Phone/i;

  function isBlocked() {
    if (MOBILE_UA.test(navigator.userAgent)) return true;
    if (window.innerWidth < MIN_WIDTH) return true;
    if (window.innerHeight < MIN_HEIGHT) return true;
    return false;
  }

  function applyState() {
    var blocker = document.getElementById('screen-blocker');
    var app = document.getElementById('app-root');
    if (!blocker || !app) return;
    if (isBlocked()) {
      blocker.style.display = 'flex';
      app.style.display = 'none';
    } else {
      blocker.style.display = 'none';
      app.style.display = '';
    }
  }

  window.addEventListener('resize', applyState);
  document.addEventListener('DOMContentLoaded', applyState);
})();
