(() => {
  'use strict';
  const root = document.getElementById('telephone-phone');
  if (!root) return;
  const $ = id => document.getElementById(id);
  const status = message => { $('phone-status').textContent = message; };
  const element = (tag, text, attrs = {}) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    return node;
  };
  const action = async (path, body, method = 'POST') => {
    const response = await fetch('/api/telephone' + path, {method,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': root.dataset.csrf},
      ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Check the entered details.');
    return result;
  };
  const run = work => async event => {
    if (event) event.preventDefault();
    try { await work(event); } catch (error) { status(error.message || 'The request could not be verified.'); }
  };
  const resetSaved = form => form.dispatchEvent(new Event('dirty-save:reset'));
  const loadSetup = async () => {
    const forms = [$('phone-route'), $('phone-policy')];
    forms.forEach(form => Array.from(form.elements).forEach(control => { control.disabled = true; }));
    try {
      const values = await Promise.all([action('/route', undefined, 'GET'), action('/policy', undefined, 'GET')]);
      forms.forEach((form, index) => Object.entries(values[index]).forEach(([name, value]) => {
        const control = form.elements.namedItem(name);
        if (!control) return;
        if (control.type === 'checkbox') control.checked = value === true;
        else control.value = value === null ? '' : String(value);
      }));
    } finally {
      forms.forEach(form => {
        Array.from(form.elements).forEach(control => { control.disabled = false; });
        resetSaved(form);
      });
    }
  };
  const field = () => {
    const row = element('div', undefined, {class: 'grid gap-2 sm:grid-cols-3 border rounded p-3', 'data-phone-field': ''});
    const input = (key, label, type = 'text') => {
      const wrap = element('label', label); const control = element('input', undefined, {type, 'data-key': key, class: 'block w-full border rounded p-2'});
      if (type !== 'checkbox') control.required = key !== 'success_value';
      wrap.append(control); row.append(wrap); return control;
    };
    input('key', 'Field key'); input('label', 'Question or label');
    const wrap = element('label', 'Type'); const select = element('select', undefined, {'data-key': 'type', class: 'block w-full border rounded p-2'});
    ['text', 'number', 'integer', 'boolean', 'date', 'email', 'choices', 'object', 'array'].forEach(type => select.append(element('option', type, {value: type})));
    wrap.append(select); row.append(wrap); input('required', 'Required to complete', 'checkbox');
    input('success_value', 'Must equal for success (optional)');
    const choices = input('choices', 'Choices, separated by commas'); choices.required = false;
    const remove = element('button', 'Remove field', {type: 'button', class: 'border rounded p-2'}); remove.onclick = () => row.remove(); row.append(remove);
    $('phone-fields').append(row);
  };
  $('phone-add-field').onclick = field;
  const templateSpec = form => {
    const properties = {}, required = [], successProperties = {};
    $('phone-fields').querySelectorAll('[data-phone-field]').forEach(row => {
      const value = key => row.querySelector('[data-key="' + key + '"]');
      const key = value('key').value.trim(), type = value('type').value;
      if (!/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(key) || Object.hasOwn(properties, key)) throw new Error('Use a unique field key beginning with a letter.');
      const schema = {title: value('label').value, type: ['text', 'date', 'email', 'choices'].includes(type) ? 'string' : type};
      if (type === 'date' || type === 'email') schema.format = type;
      if (type === 'text') schema.maxLength = 2000;
      if (type === 'choices') { schema.enum = value('choices').value.split(',').map(x => x.trim()).filter(Boolean); if (!schema.enum.length) throw new Error('Enter the choices for this field.'); }
      properties[key] = schema;
      if (value('required').checked) required.push(key);
      const target = value('success_value').value;
      if (target !== '') {
        let expected = target;
        if (['number', 'integer', 'boolean', 'object', 'array'].includes(type)) expected = JSON.parse(target);
        successProperties[key] = {const: expected};
      }
    });
    const fields = form.advanced_fields.value.trim() ? JSON.parse(form.advanced_fields.value) : {type: 'object', properties, required, additionalProperties: false};
    const success = form.advanced_success.value.trim() ? JSON.parse(form.advanced_success.value) : {type: 'object', properties: successProperties, required: Object.keys(successProperties)};
    return {name: form.elements.namedItem('name').value, objective: form.objective.value, provided_facts: {notes: form.provided_notes.value}, fields_schema: fields, success_schema: success,
      permitted_actions: ['record_fields', 'end_call', ...(form.save_message.checked ? ['save_message'] : []), ...(form.request_handover.checked ? ['request_handover'] : [])],
      required_receipts: ['message_delivered', 'handover_answered'].filter(key => form[key].checked)};
  };
  let savedTemplates = [];
  const loadTemplates = async () => {
    const result = await action('/templates', undefined, 'GET'); savedTemplates = result.templates || [];
    $('phone-purpose').replaceChildren(element('option', 'Choose a saved purpose', {value: ''}));
    savedTemplates.forEach((item, index) => $('phone-purpose').append(element('option', item.spec.name + ' · version ' + item.version, {value: String(index)})));
  };
  $('phone-template').onsubmit = run(async () => { await action('/templates', {spec: templateSpec($('phone-template'))}); await loadTemplates(); status('New purpose version saved. Existing approvals keep their original version.'); });
  $('phone-purpose').onchange = () => {
    const selected = savedTemplates[Number($('phone-purpose').value)];
    $('phone-preview').textContent = $('phone-purpose').value === '' ? '' : JSON.stringify({objective: selected.spec.objective, provided_facts: selected.spec.provided_facts, information: selected.spec.fields_schema, success: selected.spec.success_schema}, null, 2);
  };
  $('phone-prepare').onsubmit = run(async () => {
    const form = $('phone-prepare'), selected = savedTemplates[Number($('phone-purpose').value)];
    if ($('phone-purpose').value === '' || !selected) throw new Error('Choose the exact purpose version.');
    await action('/outbound', {idempotency_key: crypto.randomUUID(), to_number: form.to_number.value, from_number: form.from_number.value, script: form.script.value,
      max_duration_seconds: Number(form.max_duration_seconds.value), template_id: selected.id, template_version: selected.version});
    status('Prepared. Review the exact request, then approve it; approval does not start the call.'); await refresh();
  });
  $('phone-route').onsubmit = run(async () => {
    const form = $('phone-route'); const body = Object.fromEntries(new FormData(form));
    body.enabled = form.enabled.checked; body.transfer_verified = form.transfer_verified.checked;
    body.transfer_number = form.transfer_number.value || null; body.transfer_timeout_seconds = Number(form.transfer_timeout_seconds.value); body.forwarding_state = 'unknown';
    await action('/route', body); resetSaved(form); status('Inbound setup saved. Forwarding and real-call testing are separate.');
  });
  $('phone-policy').onsubmit = run(async () => { const form = $('phone-policy'); const body = {enabled: form.enabled.checked}; ['daily_budget_minor', 'max_call_cost_minor', 'max_concurrent', 'max_duration_seconds'].forEach(key => body[key] = Number(form[key].value)); await action('/policy', body); resetSaved(form); status('Explicit call limits saved.'); });
  $('phone-eligibility').onsubmit = run(async () => { const form = $('phone-eligibility'); await action('/eligibility', {phone: form.phone.value, eligible: true, callback_requested: form.callback_requested.checked, expires_at: new Date(form.expires_at.value).toISOString(), basis: 'owner_attested_callback'}); status('Requested callback eligibility recorded; suppression is still checked at dispatch.'); });
  $('phone-connect').onclick = run(async () => { if (!confirm('Replace this provider number’s voice webhook? SMS and carrier forwarding will not change.')) return; await action('/connect-number', {}); status('Provider voice webhook configured. Make a supervised routing test before claiming the service works.'); });
  $('phone-offboard').onclick = run(async () => { await action('/offboard', {restored: false}); status('New incoming routing stopped. Restore and test carrier forwarding; active calls retain their records.'); });
  $('phone-attest').onclick = run(async () => { if (!$('phone-restored').checked) throw new Error('Complete the carrier restoration and handset check first.'); await action('/offboard', {restored: true}); status('Your restoration check is recorded. This is not independent carrier verification.'); });
  const refresh = async () => {
    const result = await action('/calls', undefined, 'GET'); $('phone-calls').replaceChildren();
    for (const row of result.calls || []) {
      const card = element('article', undefined, {class: 'border rounded p-3 space-y-2'});
      card.append(element('p', (row.direction || 'outbound') + ' · Call connection: ' + row.status + (row.cancel_requested ? ' · cancellation requested' : '')));
      const task = row.objective_state || {};
      card.append(element('p', 'Business task: ' + (task.status || 'not evaluated') + ' · Message delivery: ' + (task.message_delivery || 'not verified')));
      card.append(element('p', 'Cost: ' + (row.cost_minor === null || row.cost_minor === undefined ? 'unresolved' : row.cost_minor) + ' · reservation: ' + row.reserved_minor));
      const button = (label, path, extra) => { const node = element('button', label, {type: 'button', class: 'border rounded p-2 mr-2'}); node.onclick = run(async () => { const data = await action('/calls/' + row.id + path, extra || {}); if (path === '') { const detail = element('pre', JSON.stringify(data, null, 2), {class: 'whitespace-pre-wrap break-words text-xs'}); card.append(detail); } else { status('Recorded: ' + (data.status || 'updated') + '.'); await refresh(); } }); card.append(node); };
      const inspect = element('button', 'Review request and evidence', {type: 'button', class: 'border rounded p-2 mr-2'});
      inspect.onclick = run(async () => { const data = await action('/calls/' + row.id, undefined, 'GET'); const snap = data.snapshot || {}; const goal = data.objective_state || {};
        card.append(element('pre', JSON.stringify({recipient: snap.to_number, outgoing_number: snap.from_number, purpose: (snap.template || {}).name,
          purpose_version: snap.template_version, objective: snap.objective, script: snap.script, duration_limit_seconds: snap.max_duration_seconds,
          task_status: goal.status || 'not evaluated', incomplete_reason: goal.reason, gathered_answers: goal.facts || {}, answer_evidence: goal.evidence || {},
          message: data.message_text, message_confirmed: data.message_confirmed, handover_status: (data.transfer || {}).status || 'not requested',
          connection_does_not_verify_human: true, usage_complete: data.usage_complete, cost: data.cost_minor}, null, 2), {class: 'whitespace-pre-wrap break-words text-xs'})); }); card.append(inspect);
      if (row.status === 'pending_approval') button('Approve this request', '/approve');
      if (row.status === 'approved') button('Start approved call', '/dispatch');
      if (!['completed', 'failed', 'busy', 'no_answer', 'cancelled'].includes(row.status)) button('Request cancellation', '/cancel');
      button('Retry message delivery', '/deliver-message');
      const erase = element('button', 'Erase captured message', {type: 'button', class: 'border rounded p-2'}); erase.onclick = run(async () => { if (!confirm('Erase the captured message and its CRM text? This does not erase independent purpose answers or call receipts.')) return; await action('/calls/' + row.id + '/message', undefined, 'DELETE'); status('Captured message erased.'); await refresh(); }); card.append(erase);
      $('phone-calls').append(card);
    }
  };
  $('phone-refresh').onclick = run(refresh);
  run(async () => { await loadSetup(); await loadTemplates(); await refresh(); status('Phone configuration loaded. Real calls are not certified by this page.'); })();
})();
