'use strict';
/* Voice uses the wizard's form and the shared dock. A reviewed suggestion
   changes a form draft; the page's own Continue performs the guarded save. */
window.Blam8VoiceGuide = function ({messages, composer, input, sendButton, scope, pageAction, choose}) {
  const form = document.getElementById('voice-form');
  const fields = {
    tone: {label: 'Tone of voice', question: 'How should your replies sound?',
      choices: ['Warm and professional', 'Friendly and casual', 'Direct and concise']},
    agent_name: {label: 'Assistant name', question: 'What should the assistant call itself?',
      choices: ['The office team', 'Customer services']},
    sign_off: {label: 'Sign-off', question: 'How would you like to sign off?',
      choices: ['Kind regards', 'Best wishes', 'Thanks']},
    voice_notes: {label: 'Writing notes'}
  };
  const storageKey = 'blam8:voice-review:' + scope;
  const version = form?.elements.voice_expected.value;
  const card = document.createElement('section');
  card.className = 'space-y-3 rounded-xl border border-slate-200 bg-white p-3 text-sm';
  card.dataset.voiceGuide = '';
  const hint = document.createElement('p');
  hint.className = 'mb-2 text-xs text-slate-600';
  hint.setAttribute('role', 'status');
  hint.hidden = true;
  composer.prepend(hint);
  const nextButton = document.createElement('button');
  nextButton.type = 'button';
  nextButton.textContent = 'Next';
  nextButton.className = 'h-9 rounded-xl bg-brand px-3 text-sm font-semibold text-white';
  sendButton.after(nextButton);
  nextButton.addEventListener('click', next);
  input.placeholder = 'Ask or answer…';
  let pending = {}, working = false, lastView = '';
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey));
    if (form && saved?.version === version) {
      for (const key of Object.keys(fields)) {
        if (typeof saved.pending?.[key] === 'string') pending[key] = saved.pending[key];
      }
    }
  } catch (_) { /* Browser storage is optional. */ }
  function persist() {
    try {
      if (Object.keys(pending).length) sessionStorage.setItem(storageKey, JSON.stringify({version, pending}));
      else sessionStorage.removeItem(storageKey);
    } catch (_) { /* Keep the active review in memory. */ }
  }
  function current() {
    if (!form || form.classList.contains('hidden')) return null;
    return Object.keys(fields).find(key => form.elements[key]?.required && !form.elements[key].value.trim());
  }
  function question() {
    const reviews = Object.entries(pending);
    if (reviews.length) return 'Suggestions awaiting review: ' + reviews.map(([key, value]) => fields[key].label + ': ' + value).join('; ');
    return fields[current()]?.question;
  }
  function say(message) {
    const status = document.getElementById('voice-completion-status');
    if (status && status.textContent === hint.textContent && status.textContent) status.textContent = '';
    hint.hidden = !message;
    hint.textContent = message;
  }
  function line(parent, tag, text, classes) {
    const el = document.createElement(tag);
    el.className = classes;
    el.textContent = text;
    parent.append(el);
    return el;
  }
  function action(parent, text, classes, handle) {
    const button = line(parent, 'button', text, classes);
    button.type = 'button';
    button.disabled = working;
    button.addEventListener('click', handle);
    return button;
  }
  function fillField(key, value) {
    if (form.classList.contains('hidden')) document.getElementById('voice-adjust')?.click();
    const field = form.elements[key];
    field.value = value;
    field.dispatchEvent(new Event('input', {bubbles:true}));
    field.dispatchEvent(new Event('change', {bubbles:true}));
  }
  function finish(key, accept) {
    const value = pending[key];
    if (accept && !value.trim()) { say('Enter a value or choose Leave.'); return; }
    delete pending[key]; persist(); say('');
    if (accept) fillField(key, value);
    lastView = '';
    choose(accept ? 'Accepted ' + fields[key].label + ': ' + value : 'Left ' + fields[key].label + ' unchanged.');
  }
  function review(key) {
    const row = line(card, 'div', '', 'space-y-1');
    row.dataset.voiceSuggestion = key;
    line(row, 'strong', fields[key].label, 'block text-sm text-navy');
    const value = line(row, 'p', pending[key], 'whitespace-pre-wrap break-words text-slate-700');
    const actions = line(row, 'div', '', 'flex items-center gap-3');
    action(actions, 'Edit', 'py-1 text-brand', () => {
      if (row.querySelector('textarea')) { row.querySelector('textarea').focus(); return; }
      const editor = document.createElement('textarea');
      editor.className = 'w-full rounded-lg border border-slate-300 p-2';
      editor.setAttribute('aria-label', 'Edit ' + fields[key].label);
      editor.value = pending[key];
      editor.addEventListener('input', () => { pending[key] = editor.value; persist(); });
      value.replaceWith(editor); editor.focus();
    });
    action(actions, 'Leave', 'py-1 text-slate-600', () => finish(key, false));
    action(actions, 'Accept', 'ml-auto py-1 text-brand', () => finish(key, true));
  }
  function refresh() {
    const keys = Object.keys(pending), key = current();
    const typed = !!input.value.trim();
    sendButton.style.display = typed ? '' : 'none';
    nextButton.hidden = typed;
    const page = pageAction();
    const blocked = working || keys.length > 0 || !page || page.disabled;
    nextButton.setAttribute('aria-disabled', String(blocked));
    nextButton.classList.toggle('opacity-50', blocked);
    const view = JSON.stringify([keys, key, working, !!form, form?.classList.contains('hidden')]);
    if (view === lastView) return;
    lastView = view;
    card.replaceChildren();
    if (keys.length) {
      line(card, 'strong', 'Suggested changes', 'block text-navy');
      keys.forEach(review);
    } else if (key) {
      line(card, 'strong', fields[key].question, 'block text-navy');
      line(card, 'p', 'Choose an answer or write your own.', 'text-xs text-slate-500');
      const choices = line(card, 'div', '', 'flex flex-wrap gap-2');
      fields[key].choices.forEach(answer => {
        action(choices, answer, 'rounded-lg border border-slate-200 px-3 py-2 text-left text-navy', () => {
          say('');
          fillField(key, answer);
          choose(answer);
        });
      });
    } else {
      line(card, 'p', !form ? 'Learning your voice from sent emails.' :
        form.classList.contains('hidden') ? 'Review the learned style in the form. Choose Next to use it, or adjust the settings.' :
        'Your reply style is ready. Choose Next to save and continue to Knowledge.', 'text-slate-700');
    }
  }
  function blocked() {
    if (Object.keys(pending).length) { say('Accept, edit or leave the suggested changes before continuing.'); return true; }
    if (working) { say('Waiting for the assistant’s reply.'); return true; }
    return false;
  }
  function next() {
    if (blocked()) return;
    const button = pageAction();
    if (button && !button.disabled) button.click();
    else say(window.Blam8VoiceCompletion?.read().reason || 'Wait for the voice review or open manual setup.');
  }
  document.addEventListener('submit', event => {
    if ((event.target === form || event.target.matches('[data-voice-accept]')) && blocked()) {
      event.preventDefault(); event.stopImmediatePropagation();
      const status = document.getElementById('voice-completion-status');
      if (status) status.textContent = hint.textContent;
    }
  }, true);
  input.addEventListener('input', () => { say(''); refresh(); });
  return {
    question, refresh, next,
    mount() { messages.append(card); refresh(); },
    beginTurn() { if (working) return false; working = true; say(''); refresh(); return true; },
    endTurn() { working = false; refresh(); },
    propose(fill) {
      for (const key of Object.keys(fields)) {
        if (form && typeof fill[key] === 'string' && fill[key].trim() && fill[key] !== form.elements[key].value)
          pending[key] = fill[key];
      }
      persist(); lastView = '';
    }
  };
};
