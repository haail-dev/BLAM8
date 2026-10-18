/* BLAM8 PWA client: service-worker registration, push subscription, the
 * enable-notifications banner, the install entry point, and the offline banner.
 * Loaded (deferred) on every /app page from src/app/templates/app/base.html.
 *
 * Quiet by design: if the browser lacks SW/Push support, or the VAPID key isn't
 * configured server-side, everything below no-ops without console noise.
 */
(function () {
  'use strict';

  var vapidMeta = document.querySelector('meta[name="blam8-vapid-key"]');
  var VAPID_KEY = vapidMeta ? vapidMeta.content : '';

  function csrfToken() {
    var m = document.cookie.match(/(?:^|; )bl_csrf=([^;]*)/);
    return m ? decodeURIComponent(m[1]) : '';
  }

  function urlBase64ToUint8Array(base64String) {
    var padding = '='.repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, '+').replace(/_/g, '/');
    var raw = atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  // ── Service worker ────────────────────────────────────────────────────────
  var swReady = null;
  if ('serviceWorker' in navigator) {
    swReady = navigator.serviceWorker.register('/sw.js').catch(function () { return null; });
    // A tapped notification whose own navigation failed (sw.js): go to what
    // it was about — the draft, not whatever screen was open (Kris
    // 2026-09-24: "it stayed in the VC"). Same-origin paths only.
    navigator.serviceWorker.addEventListener('message', function (e) {
      var d = e.data || {};
      if (d.type !== 'blam8:open' || typeof d.url !== 'string') return;
      if (d.url.charAt(0) !== '/' || d.url.charAt(1) === '/') return;
      if (location.pathname + location.search === d.url) return;
      location.assign(d.url);
    });
  }

  // The tap, remembered (sw.js writes it on notificationclick). Where iOS
  // does run the click handler (a closed app tapped awake), the page that
  // opens reads it and goes there. On an iPhone app that is ALREADY running
  // iOS runs nothing on a tap (ten live tests, 2026-09-24) — Settings says
  // so. Read once, then cleared.
  function goWhereTapped() {
    if (!('caches' in window)) return Promise.resolve(false);
    return caches.open('blam8-open').then(function (c) {
      return c.match('/__blam8_open').then(function (r) {
        if (!r) return false;
        return r.json().then(function (d) {
          c.delete('/__blam8_open');
          var url = d && d.url;
          if (typeof url !== 'string' || url.charAt(0) !== '/' || url.charAt(1) === '/') return false;
          if (!(Date.now() - (d.at || 0) < 60000)) return false;
          if (location.pathname + location.search !== url) location.assign(url);
          return true;   // a tapped notification owns this landing (concierge_panel.js leaves it alone)
        });
      });
    }).catch(function () { return false; });
  }
  window.blam8TapLanding = goWhereTapped;   // resolves true when a fresh notification tap owns this landing
  goWhereTapped();
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible') return;
    goWhereTapped();
    // An app left open keeps the worker it started with until a reload;
    // coming forward checks for a newer one.
    if ('serviceWorker' in navigator) {
      navigator.serviceWorker.getRegistration().then(function (r) { if (r) r.update(); }).catch(function () {});
    }
  });
  window.addEventListener('pageshow', goWhereTapped);

  // ── Push subscription ─────────────────────────────────────────────────────
  var pushSupported = !!(swReady && 'PushManager' in window && 'Notification' in window && VAPID_KEY);

  function subscribe() {
    return navigator.serviceWorker.ready.then(function (reg) {
      return reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(VAPID_KEY),
      });
    }).then(function (sub) {
      return fetch('/api/push/subscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken() },
        body: JSON.stringify(sub.toJSON()),
      });
    });
  }

  // Permission already granted (returning device): quietly refresh the
  // subscription so the server always has a live endpoint.
  if (pushSupported && Notification.permission === 'granted') {
    subscribe().catch(function () { /* best-effort */ });
  }

  // ── Enable-notifications banner ───────────────────────────────────────────
  // Shown once per session, max 3 dismissals ever (then never again).
  var DISMISS_KEY = 'blam8.push.dismissals';
  function dismissals() { return parseInt(localStorage.getItem(DISMISS_KEY) || '0', 10); }

  if (pushSupported && Notification.permission === 'default' &&
      dismissals() < 3 && !sessionStorage.getItem('blam8.push.seen')) {
    sessionStorage.setItem('blam8.push.seen', '1');
    var banner = document.createElement('div');
    // bottom-4 is the desktop resting place; base.html re-anchors #blam8-push-banner
    // just above the strip/diary rail on phones (and above the rail on desktop).
    banner.id = 'blam8-push-banner';
    banner.className = 'fixed bottom-4 inset-x-4 sm:left-auto sm:right-6 sm:max-w-sm z-50 ' +
      'rounded-xl bg-white border border-slate-200 shadow-xl p-4 flex items-start gap-3';
    banner.innerHTML =
      '<img src="/static/icons/icon-96.png" alt="" class="h-9 w-9 rounded-lg flex-none">' +
      '<div class="min-w-0 flex-1">' +
      '  <p class="text-sm font-semibold text-navy">Get notified when drafts are ready</p>' +
      '  <div class="mt-2 flex gap-2">' +
      '    <button type="button" data-act="enable" class="rounded-lg bg-brand px-3 py-1.5 text-xs font-semibold text-white hover:bg-blue-700 transition">Enable notifications</button>' +
      '    <button type="button" data-act="later" class="rounded-lg px-3 py-1.5 text-xs font-semibold text-slate-500 hover:bg-slate-100 transition">Maybe later</button>' +
      '  </div>' +
      '</div>';
    banner.addEventListener('click', function (e) {
      var act = e.target && e.target.getAttribute('data-act');
      if (act === 'enable') {
        subscribe().catch(function () { /* denied or failed — don't pester */ });
        banner.remove();
      } else if (act === 'later') {
        localStorage.setItem(DISMISS_KEY, String(dismissals() + 1));
        banner.remove();
      }
    });
    document.body.appendChild(banner);
  }

  // ── Install entry point ───────────────────────────────────────────────────
  // The browser fires beforeinstallprompt when the app is installable and not
  // yet installed; we surface the deferred prompt behind the "Install BLAM8"
  // menu item (hidden by default in base.html). iOS never fires it — there the
  // item explains Share → "Add to Home Screen".
  var installItem = document.getElementById('install-app');
  var deferredPrompt = null;
  var isStandalone = window.matchMedia('(display-mode: standalone)').matches ||
                     window.navigator.standalone === true;

  window.addEventListener('beforeinstallprompt', function (e) {
    e.preventDefault();
    deferredPrompt = e;
    if (installItem && !isStandalone) installItem.classList.remove('hidden');
  });

  var isIOS = /iphone|ipad|ipod/i.test(navigator.userAgent) ||
              (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  if (installItem && isIOS && !isStandalone) installItem.classList.remove('hidden');

  if (installItem) {
    installItem.addEventListener('click', function (e) {
      e.preventDefault();
      if (deferredPrompt) {
        deferredPrompt.prompt();
        deferredPrompt.userChoice.then(function () {
          deferredPrompt = null;
          installItem.classList.add('hidden');
        });
      } else if (isIOS) {
        alert('To install BLAM8: tap the Share button, then "Add to Home Screen".');
      }
    });
  }

  window.addEventListener('appinstalled', function () {
    if (installItem) installItem.classList.add('hidden');
  });

  // ── Offline banner ────────────────────────────────────────────────────────
  var offlineBar = null;
  function setOffline(off) {
    if (off && !offlineBar) {
      offlineBar = document.createElement('div');
      offlineBar.className = 'fixed top-0 inset-x-0 z-50 bg-amber-500 text-white text-center text-sm font-medium py-1.5';
      offlineBar.textContent = "You're offline — changes will sync when reconnected";
      document.body.appendChild(offlineBar);
    } else if (!off && offlineBar) {
      offlineBar.remove();
      offlineBar = null;
    }
  }
  window.addEventListener('offline', function () { setOffline(true); });
  window.addEventListener('online', function () { setOffline(false); });
  if (navigator.onLine === false) setOffline(true);
})();
