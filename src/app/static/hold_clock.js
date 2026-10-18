/* The offer countdown pill in the thread's held list (Kris 2026-09-27,
   filming: "instead of 'Held until…' just have a countdown; if clicked it
   explains what it is using a tour bubble").

   Every [data-hold-clock data-until=ISO] shows the time left on its offer,
   refreshed every 30 s. A tap opens one bubble in the tour's style
   (.tour-bubble in app/base.html) with the button's data-explain text; a tap
   anywhere else, Escape, or a scroll closes it. Delegated from document, so
   the live thread repaint (live.js swaps #thread-live) needs no re-binding. */
(function () {
  "use strict";
  if (window.blam8HoldClock) return;
  window.blam8HoldClock = true;

  function left(iso) {
    var ms = new Date(iso).getTime() - Date.now();
    if (!isFinite(ms)) return "";
    var mins = Math.floor(ms / 60000);
    if (mins <= 0) return "releasing";
    if (mins < 60) return mins + "m";
    var m = mins % 60;
    return Math.floor(mins / 60) + "h " + (m < 10 ? "0" : "") + m + "m";
  }

  function tick() {
    var pills = document.querySelectorAll("[data-hold-clock]");
    for (var i = 0; i < pills.length; i++) {
      var out = pills[i].querySelector("[data-left]");
      var t = left(pills[i].getAttribute("data-until"));
      if (out && t) out.textContent = t;
    }
  }

  var bubble = null, owner = null;
  function close() {
    if (bubble) { bubble.remove(); bubble = null; owner = null; }
  }

  function open(btn) {
    close();
    owner = btn;
    bubble = document.createElement("div");
    bubble.className = "tour-bubble bg-white border border-slate-200 rounded-xl shadow-xl";
    bubble.setAttribute("role", "dialog");
    var t = document.createElement("p");
    t.className = "text-sm font-semibold text-navy";
    t.textContent = "Held for them: " + (btn.querySelector("[data-left]") || {}).textContent + " left";
    var p = document.createElement("p");
    p.className = "mt-1 text-sm leading-relaxed text-slate-600";
    p.textContent = btn.getAttribute("data-explain") || "";
    bubble.appendChild(t);
    bubble.appendChild(p);
    document.body.appendChild(bubble);
    var r = btn.getBoundingClientRect(), bw = bubble.offsetWidth, bh = bubble.offsetHeight;
    var x = Math.max(8, Math.min(r.left + r.width / 2 - 24, window.innerWidth - bw - 8));
    var below = r.bottom + 10 + bh < window.innerHeight;
    bubble.style.left = x + "px";
    bubble.style.top = (below ? r.bottom + 10 : r.top - bh - 10) + "px";
    if (!below) bubble.classList.add("tour-bubble-above");
    bubble.style.setProperty("--tour-arrow-x", Math.max(12, r.left + r.width / 2 - x - 6) + "px");
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest && e.target.closest("[data-hold-clock]");
    if (btn) {
      e.preventDefault();
      if (owner === btn) close(); else open(btn);
      return;
    }
    if (bubble && !bubble.contains(e.target)) close();
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
  window.addEventListener("scroll", close, true);

  tick();
  setInterval(tick, 30000);
})();
