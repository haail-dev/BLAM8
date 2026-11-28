'use strict';
/* Field choices update wizard drafts. The visible form owns validation and saving. */
window.Blam8FieldGuide = function ({step, messages, composer, input, sendButton, scope, pageAction, choose}) {
  const interview = step === 'interview';
  const form = document.getElementById(interview ? 'interview-form' : 'voice-form');
  const fields = interview ? {
    industry: {label: 'Business type', question: 'How would you describe your type of business?'},
    business_name: {label: 'Business name', question: 'What name do your customers know you by?'},
    business_brief: {label: 'Business description', question: 'What do you offer, and who are your customers?'},
    goals: {label: 'What you want help with', question: 'What would you like BLAM8 to help with?',
      choices: ['Answer enquiries', 'Manage bookings', 'Follow up quotes']},
    website: {label: 'Website'}
  } : {
    tone: {label: 'Tone of voice', question: 'How should your replies sound?',
      choices: ['Warm and professional', 'Friendly and casual', 'Direct and concise']},
    agent_name: {label: 'Assistant name', question: 'What should the assistant call itself?',
      choices: ['The office team', 'Customer services']},
    sign_off: {label: 'Sign-off', question: 'How would you like to sign off?',
      choices: ['Kind regards', 'Best wishes', 'Thanks']},
    voice_notes: {label: 'Writing notes'}
  };
  const storageKey = 'blam8:' + (interview ? 'interview' : 'voice') + '-review:' + scope;
  const version = interview && form
    ? JSON.stringify(Object.keys(fields).map(key => form.elements[key].defaultValue))
    : form?.elements.voice_expected.value;
  const card = document.createElement('section');
  card.className = 'guided-question';
  card.dataset.fieldGuide = '';
  messages.classList.add('field-guide-stream');
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
    return Object.keys(fields).find(key => fields[key].question && !form.elements[key].value.trim());
  }
  function question() {
    const reviews = Object.entries(pending);
    if (reviews.length) return 'Suggestions awaiting review: ' + reviews.map(([key, value]) => fields[key].label + ': ' + value).join('; ');
    return fields[current()]?.question;
  }
  function say(message) {
    const status = document.getElementById(interview ? 'interview-guide-status' : 'voice-completion-status');
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
    if (!interview && form.classList.contains('hidden')) document.getElementById('voice-adjust')?.click();
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
    choose(accept ? 'Accepted ' + fields[key].label + ': ' + value : 'Left ' + fields[key].label + ' unchanged.', accept ? selection(key, value) : null);
  }
  function review(key) {
    const row = line(card, 'div', '', 'space-y-1');
    row.dataset.fieldSuggestion = key;
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
  function selection(key, answer) {
    const field = fields[key];
    return {step, question: field.question || field.label, choices: field.choices || [], answer};
  }
  function prompt(parent, item, completed) {
    line(parent, 'h3', item.question, 'guided-title');
    if (!completed) line(parent, 'p', item.choices.length ? 'Choose an answer or write your own.' : 'Write your answer below or enter it in the form.', 'guided-section');
    const choices = line(parent, 'div', '', 'guided-actions');
    const options = completed && !item.choices.includes(item.answer) ? [...item.choices, item.answer] : item.choices;
    options.forEach(answer => {
      if (completed) {
        const option = line(choices, 'span', answer, 'field-guide-option');
        if (answer === item.answer) {
          option.classList.add('field-guide-selected');
          const label = line(option, 'span', 'Selected: ', 'sr-only');
          option.prepend(label);
        }
      } else {
        action(choices, answer, '', () => {
          say(''); fillField(item.key, answer); choose(answer, selection(item.key, answer));
        });
      }
    });
  }
  function history(message) {
    const item = message.field_selection;
    if (message.role !== 'user' || item?.step !== step || typeof item.question !== 'string' ||
        typeof item.answer !== 'string' || !Array.isArray(item.choices) || !item.choices.every(v => typeof v === 'string')) return null;
    const block = document.createElement('section');
    block.className = 'guided-question field-guide-completed';
    prompt(block, item, true);
    return block;
  }
  function refresh() {
    const keys = Object.keys(pending), key = current();
    const typed = !!input.value.trim();
    sendButton.style.display = typed ? '' : 'none';
    nextButton.hidden = typed;
    const page = pageAction();
    const blocked = working || keys.length > 0 || !page || page.disabled || (interview && !form.elements.industry.value.trim());
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
      prompt(card, {key, question: fields[key].question, choices: fields[key].choices || []}, false);
    } else {
      line(card, 'p', interview ? 'Review your business details, then choose Next to continue to your customer inbox.' : !form ? 'Learning your voice from sent emails.' :
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
    if (interview && !form.elements.industry.value.trim()) { say('Enter your business type before continuing.'); return; }
    if (button && !button.disabled) button.click();
    else say(window.Blam8VoiceCompletion?.read().reason || 'Wait for the voice review or open manual setup.');
  }
  document.addEventListener('submit', event => {
    if ((event.target === form || event.target.matches('[data-voice-accept]')) && blocked()) {
      event.preventDefault(); event.stopImmediatePropagation();
      const status = document.getElementById(interview ? 'interview-guide-status' : 'voice-completion-status');
      if (status) status.textContent = hint.textContent;
    }
  }, true);
  input.addEventListener('input', () => { say(''); refresh(); });
  return {
    question, refresh, next, history,
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
