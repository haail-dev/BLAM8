(() => {
  const form = document.getElementById('plan-create');
  if (form) {
    const select = form.elements.kind;
    const show = () => form.querySelectorAll('[data-kinds]').forEach(el => {
      el.hidden = !el.dataset.kinds.split(' ').includes(select.value) ||
        (el.dataset.documentAction && el.dataset.documentAction !== form.elements.document_action.value);
    });
    select.addEventListener('change', show); show();
    form.elements.document_action.addEventListener('change', show);
  }
  let dragged = null;
  document.querySelectorAll('[data-plan-item]').forEach(el => {
    el.addEventListener('dragstart', event => { dragged = el; event.dataTransfer.setData('text/plain', el.dataset.planItem); });
    el.addEventListener('dragend', () => { dragged = null; });
  });
  document.querySelectorAll('[data-plan-day]').forEach(el => {
    el.addEventListener('dragover', event => { if (dragged) event.preventDefault(); });
    el.addEventListener('drop', event => {
      event.preventDefault();
      if (!dragged) return;
      // The card is displayed in plan time. The manual control handles items
      // with another timezone without silently shifting their wall-clock time.
      const zoneInput = document.querySelector('form[action="/app/plan/settings"] [name="timezone"]');
      if (dragged.dataset.planZone !== zoneInput.value) {
        alert('Use Move below for an item in a different timezone.'); return;
      }
      const move = document.getElementById('plan-move');
      move.action = '/app/plan/items/' + encodeURIComponent(dragged.dataset.planItem);
      move.elements.run_at.value = el.dataset.planDay + 'T' + dragged.dataset.planTime;
      move.submit();
    });
  });
})();
