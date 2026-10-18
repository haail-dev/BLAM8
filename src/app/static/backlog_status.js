// Refresh only the status card, leaving onboarding fields and focus intact.
(function () {
  var card = document.querySelector('[data-backlog-status]');
  if (!card) return;
  var failures = 0;
  async function refresh() {
    if (card.dataset.backlogStatus !== 'running') return;
    if (document.hidden) { setTimeout(refresh, 2500); return; }
    var controller = new AbortController();
    var timeout = setTimeout(function () { controller.abort(); }, 10000);
    try {
      var response = await fetch(card.dataset.pollUrl, {credentials: 'same-origin', cache: 'no-store', signal: controller.signal});
      if (!response.ok || response.redirected) throw new Error('Status unavailable');
      var doc = new DOMParser().parseFromString(await response.text(), 'text/html');
      var next = doc.querySelector('[data-backlog-status]');
      if (!next) throw new Error('Status unavailable');
      if (next.outerHTML !== card.outerHTML) { card.replaceWith(next); card = next; }
      failures = 0;
    } catch (error) {
      failures += 1;
      if (failures >= 3) {
        card.querySelector('[data-backlog-refresh-error]').classList.remove('hidden');
        return;
      }
    } finally { clearTimeout(timeout); }
    if (card.dataset.backlogStatus === 'running') setTimeout(refresh, 2500);
  }
  if (card.dataset.backlogStatus === 'running') setTimeout(refresh, 2500);
})();
