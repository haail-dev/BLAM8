(function () {
  'use strict';
  var section = document.getElementById('held-times');
  if (!section) return;
  var busy = false, chosen = false;
  function tick() {
    section.querySelectorAll('[data-held-row]').forEach(function (row) {
      var clock = row.querySelector('[data-held-until]');
      var left = Date.parse(clock.dataset.heldUntil) - Date.now();
      var mins = Math.max(0, Math.ceil(left / 60000));
      clock.textContent = left <= 0 ? 'Released' :
        (mins >= 60 ? Math.floor(mins / 60) + 'h ' : '') + mins % 60 + 'm left';
      row.querySelector('button').disabled = busy || chosen || left <= 0;
    });
  }
  section.addEventListener('click', async function (event) {
    var button = event.target.closest('[data-choose-offer]');
    if (!button || button.disabled || busy || chosen) return;
    busy = true; tick();
    var result = document.getElementById('held-result');
    try {
      var response = await fetch('/api/portal/' + encodeURIComponent(document.getElementById('portal').dataset.slug) + '/choose', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: section.dataset.token, booking_id: button.dataset.chooseOffer})
      });
      if (!response.ok) throw new Error('Request failed');
      var data = await response.json();
      if (data.booked) {
        chosen = true;
        result.textContent = 'Booked and confirmed.';
      } else {
        result.textContent = data.message || 'Please contact the business to choose this time.';
        if (data.error === 'offer_gone') {
          button.closest('[data-held-row]').querySelector('[data-held-until]').dataset.heldUntil = new Date(0).toISOString();
        }
      }
    } catch (_) {
      result.textContent = "We couldn't check the result. Please refresh to check your booking before trying again.";
    } finally { busy = false; tick(); }
  });
  tick(); setInterval(tick, 1000);
}());
