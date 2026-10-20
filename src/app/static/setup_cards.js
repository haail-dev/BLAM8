/* Durable setup proposals. All writes use a saved revision and never retry. */
(function () {
  "use strict";
  function node(tag, text, cls) {
    var el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function button(text, action) {
    var el = node("button", text); el.type = "button";
    el.addEventListener("click", action); return el;
  }
  function valueText(value) {
    if (value == null || value === "" || (Array.isArray(value) && !value.length)) return "Not set";
    if (typeof value === "boolean") return value ? "Yes" : "No";
    return Array.isArray(value) ? value.join(", ") : String(value);
  }
  function diff(parent, change) {
    var before = change.review_before || change.before || {}, after = change.review_after || change.after || {};
    Array.from(new Set(Object.keys(before).concat(Object.keys(after)))).forEach(function (key) {
      if (JSON.stringify(before[key]) === JSON.stringify(after[key])) return;
      parent.appendChild(node("p", (LABELS[key] || key.replace(/_/g, " ")) + ": " + valueText(before[key]) + " → " + valueText(after[key])));
    });
    (change.notes || []).forEach(function (note) { parent.appendChild(node("p", note)); });
  }
  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)bl_csrf=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }
  async function request(url, body) {
    var response = await (body && window.Blam8DeviceSync ? window.Blam8DeviceSync.request : fetch)(url, { method: body ? "POST" : "GET", cache: "no-store",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf() },
      body: body ? JSON.stringify(body) : undefined });
    var data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The request could not be confirmed. Reload the cards.");
    return data;
  }
  var LABELS = { minutes: "Duration (minutes)", price: "Price", timing: "Timing", resource: "Resource", days: "Weekdays",
    count: "Capacity", at_once: "At once", rate_unit: "Rate unit", required: "Required", services: "Applies to services", remember: "Remember",
    recall_days: "Recall days", gate: "Blocks booking until verified", options: "Options (one per line)" };
  var CHOICES = { time: "Exact time", half: "Morning or afternoon", day: "Whole day", "true": "Yes", "false": "No" };

  function Panel(options) {
    var self = this;
    this.msgs = options.messages; this.form = options.form; this.onApplied = options.onApplied || function () {};
    this.onApplying = options.onApplying || function () {};
    this.data = { cards: [], views: {}, options: {} }; this.generation = 0; this.busy = false;
    this.mode = null; this.editing = false; this.waiting = false;
    this.dots = node("div", null, "bc-setup-dots");
    this.tray = node("section", null, "bc-setup-tray"); this.tray.hidden = true;
    this.tray.setAttribute("aria-label", "Setup proposals");
    this.form.parentNode.insertBefore(this.tray, this.form);
    this.error = node("div", null, "bc-setup-error"); this.error.setAttribute("role", "status"); this.error.hidden = true;
    this.error.appendChild(this.errorText = node("span"));
    this.error.appendChild(button("Reload cards", function () { self.editing = false; self.load(); }));
    this.form.parentNode.insertBefore(this.error, this.tray);
    this.observer = new MutationObserver(function () { self.place(); });
    this.observer.observe(this.msgs, { childList: true });
    this.load();
  }
  Panel.prototype.fail = function (message) { this.errorText.textContent = message; this.error.hidden = false; };
  Panel.prototype.place = function () {
    var bubbles = this.msgs.querySelectorAll('[data-bc-role="assistant"]');
    var last = bubbles[bubbles.length - 1];
    if (last && this.dots.parentNode !== last) {
      if (this.dots.parentNode) this.dots.parentNode.removeAttribute("data-bc-setup-host");
      last.appendChild(this.dots);
    }
    if (last) last.toggleAttribute("data-bc-setup-host", !!this.dots.childElementCount && !this.waiting);
    this.dots.hidden = this.waiting || !this.dots.childElementCount;
  };
  Panel.prototype.setWaiting = function (on) {
    this.waiting = on; this.tray.hidden = on || !this.mode; this.place();
  };
  Panel.prototype.pending = function (mode) {
    var result = [], self = this;
    this.data.cards.forEach(function (card) {
      card.lines.forEach(function (line) {
        if (line.status !== "proposed" && line.status !== "held") return;
        var here = ((self.data.views[card.page] || {}).href || "").split("#")[0] === window.location.pathname;
        var held = line.status === "held" && !here;
        if ((mode === "held") === held) result.push({ card: card, line: line });
      });
    });
    return result;
  };
  Panel.prototype.load = async function () {
    var generation = ++this.generation;
    try {
      var data = await request("/concierge/setup-cards");
      if (generation !== this.generation) return;
      if (this.editing) { this.fail("Proposals may have changed. Finish or cancel this edit, then reload the cards."); return; }
      this.data = data; this.error.hidden = true; this.paint(); this.history();
    } catch (err) {
      if (generation === this.generation) {
        this.stale = true; this.fail(err.message); this.disable();
      }
    }
  };
  Panel.prototype.disable = function () {
    this.tray.querySelectorAll("button,input,select,textarea").forEach(function (el) { el.disabled = true; });
  };
  Panel.prototype.paint = function () {
    var self = this; this.stale = false; this.dots.replaceChildren();
    ["suggested", "held"].forEach(function (mode) {
      var count = self.pending(mode).length;
      if (!count) return;
      var label = mode === "held" ? "Held" : "Suggested";
      var dot = button(String(count), function () {
        if (self.editing || self.busy) return;
        self.mode = self.mode === mode ? null : mode; self.paint();
      });
      dot.className = "bc-setup-dot " + mode;
      dot.setAttribute("aria-label", label + " (" + count + ")");
      dot.setAttribute("aria-expanded", self.mode === mode ? "true" : "false"); dot.title = label;
      self.dots.appendChild(dot);
    });
    if (this.mode && !this.pending(this.mode).length) this.mode = null;
    this.tray.replaceChildren(); this.tray.hidden = !this.mode || this.waiting;
    if (this.mode) this.paintTray();
    this.place();
  };
  Panel.prototype.paintTray = function () {
    var self = this, items = this.pending(this.mode), heading = this.mode === "held" ? "Held" : "Suggested";
    var header = node("div", null, "bc-setup-heading");
    header.appendChild(node("strong", heading + " · " + items.length + " pending"));
    header.appendChild(button("Close", function () { self.mode = null; self.editing = false; self.paint(); }));
    this.tray.appendChild(header);
    var byPage = {};
    items.forEach(function (item) { (byPage[item.card.page] || (byPage[item.card.page] = [])).push(item); });
    Object.keys(byPage).forEach(function (page) {
      var view = self.data.views[page], group = byPage[page], card = group[0].card;
      var title = node("div", null, "bc-setup-heading"); title.appendChild(node("strong", view.label));
      var all = view.all;
      // Accept all means all pending lines on this page, not a hidden subset.
      var allIDs = card.lines.filter(function (l) { return l.status === "proposed" || l.status === "held"; }).map(function (l) { return l.id; });
      if (self.mode === "suggested" && all && !all.error && allIDs.length === group.length) {
        title.appendChild(button("Accept all", function () { self.accept(card, allIDs, all); }));
      }
      self.tray.appendChild(title);
      group.forEach(function (item) { self.paintLine(item, view); });
    });
  };
  Panel.prototype.paintLine = function (item, view) {
    var self = this, line = item.line, card = item.card, row = node("article", null, "bc-setup-line");
    row.dataset.lineId = line.id;
    var preview = view.rows[line.id], effective = preview && !preview.error ? preview.changes[0] : null;
    if (!effective && view.all && !view.all.error) effective = view.all.changes.find(function (v) { return v.id === line.id; });
    row.appendChild(node("p", effective ? effective.display : line.display));
    if (effective) (effective.notes || []).forEach(function (note) { row.appendChild(node("p", note)); });
    if (effective) {
      var changes = node("details"); changes.appendChild(node("summary", "Changes")); diff(changes, effective); row.appendChild(changes);
    }
    var evidence = node("details"); evidence.appendChild(node("summary", "Sources"));
    Object.keys(line.sources).forEach(function (field) {
      var source = line.sources[field];
      evidence.appendChild(node("p", field + ": " + source.kind.replace(/_/g, " ") + " — " + source.reference));
    });
    row.appendChild(evidence);
    if (preview && preview.error) row.appendChild(node("p", preview.error, "bc-setup-problem"));
    else if (!effective) (line.problems || []).forEach(function (p) { row.appendChild(node("p", p, "bc-setup-problem")); });
    var actions = node("div", null, "bc-setup-actions");
    if (this.mode === "suggested" && preview && !preview.error) actions.appendChild(button("Accept", function () { self.accept(card, [line.id], preview); }));
    actions.appendChild(button("Edit", function () { self.edit(row, card, line, effective && effective.fields); }));
    actions.appendChild(button("Leave", function () { self.mutate(card, "/changes", { revision: card.revision, changes: [{ op: "remove", id: line.id }] }); }));
    if (this.mode === "held" || (preview && preview.error)) {
      var link = node("a", "Open page"); link.href = view.href; actions.appendChild(link);
    }
    row.appendChild(actions); this.tray.appendChild(row);
  };
  Panel.prototype.mutate = async function (card, action, body) {
    if (this.busy || this.stale || this.waiting) return;
    this.busy = true; this.disable(); ++this.generation;
    try {
      if (action === "/accept") this.onApplying(card);
      var result = await request("/concierge/setup-cards/" + encodeURIComponent(card.page) + action, body);
      this.editing = false;
      if (action === "/accept") this.onApplied(result.card);
      await this.load();
    } catch (err) {
      // A lost response is uncertain, not a failed save to repeat. Keep the
      // proposed editor visible and require a fresh read before any next write.
      this.stale = true; this.fail(err.message + " Reload to check what was kept.");
    } finally { this.busy = false; }
  };
  Panel.prototype.accept = function (card, ids, preview) {
    this.mutate(card, "/accept", { revision: card.revision, ids: ids, settings_version: preview.settings_version });
  };
  Panel.prototype.edit = function (row, card, line, effective) {
    if (this.editing || this.busy || this.stale) return;
    this.editing = true; var self = this, dirty = {}, values = Object.assign({}, effective || {}, line.fields);
    var kind = line.name === "field" ? (card.page === "booking_details" ? "intake" : "subject") : line.name;
    var editor = node("div", null, "bc-setup-editor");
    var scalar = kind === "setting" && (this.data.options.settings || {})[values.key];
    if (kind === "setting" && !scalar) { this.editing = false; this.fail("This setting cannot be edited here. Leave it and request a supported setting."); return; }
    var controls = scalar ? {value: scalar.type === "choice" ? scalar.choices : scalar.type === "dates" ? "list" : scalar.type === "timezone" ? "text" : scalar.type} : (this.data.options.controls || {})[kind];
    if (!controls) { this.editing = false; this.fail("The editor controls could not be verified. Reload the cards."); return; }
    Object.keys(controls).forEach(function (key) {
      if (kind === 'ladder' && key === 'staff_can_see' && values.who !== 'contact') return;
      var stay = (self.data.views[card.page] || {}).stay;
      if (key === "at_once" && !stay) return;
      var type = controls[key], label = node("label", scalar ? scalar.label : LABELS[key] || key.replace(/_/g, " ")), control;
      if (kind === "service" && stay && key === "timing") type = ["time"];
      if (kind === "service" && stay && key === "resource") type = [String(values.name || "")];
      var value = values[key], read;
      if (type === "days") {
        control = node("div", null, "bc-setup-days");
        ["mon", "tue", "wed", "thu", "fri", "sat", "sun"].forEach(function (day) {
          var dayLabel = node("label", day), box = node("input"); box.type = "checkbox";
          box.value = day; box.checked = Array.isArray(value) && value.indexOf(day) >= 0;
          dayLabel.prepend(box); control.appendChild(dayLabel);
        });
        read = function () { return Array.from(control.querySelectorAll("input:checked")).map(function (el) { return el.value; }); };
      } else if (Array.isArray(type) || type === "boolean" || ["resources", "services", "groups"].indexOf(type) >= 0) {
        control = node("select");
        var choices = Array.isArray(type) ? type.map(String) : type === "boolean" ? ["true", "false"] : (self.data.options[type] || []).slice();
        if (type === "services") { control.multiple = true; control.size = Math.min(5, Math.max(2, choices.length)); }
        else if (choices.indexOf("") < 0) choices.unshift("");
        var actual = type === "services" ? (value || []) : value == null ? [] : [String(value)];
        actual.forEach(function (v) { if (choices.indexOf(v) < 0) choices.push(v); });
        choices.forEach(function (v) { var opt = node("option", v === "" ? "Not specified" : CHOICES[v] || v); opt.value = v; opt.selected = actual.indexOf(v) >= 0; control.appendChild(opt); });
        read = function () { return type === "services" ? Array.from(control.selectedOptions).map(function (o) { return o.value; }) : type === "boolean" ? (control.value === "" ? null : control.value === "true") : control.value; };
      } else {
        control = node(type === "list" || type === "prose" ? "textarea" : "input");
        if (type === "number" || type === "money") { control.type = "number"; control.step = type === "money" ? "0.01" : "1"; }
        if (type === "time") control.type = "time";
        if (scalar && scalar.minimum != null) control.min = scalar.minimum;
        if (scalar && scalar.maximum != null) control.max = scalar.maximum;
        control.value = Array.isArray(value) ? value.join("\n") : value == null ? "" : String(value);
        read = function () { return type === "list" ? control.value.split("\n").map(function (s) { return s.trim(); }).filter(Boolean) : type === "number" || type === "money" ? (control.value === "" ? null : Number(control.value)) : control.value; };
      }
      if (scalar) {
        var baseRead = read;
        read = function () {
          var value = baseRead();
          if (scalar.type === "choice" && typeof scalar.choices[0] === "number" && value !== "") value = Number(value);
          return scalar.nullable && value === "" ? null : value;
        };
        if (scalar.nullable) {
          var clearLabel = node("label", "Not set"), clear = node("input"); clear.type = "checkbox";
          clear.checked = value === null; clear.dataset.clearSetting = ""; control.disabled = clear.checked;
          var valueRead = read; read = function () { return clear.checked ? null : valueRead(); };
          clear.addEventListener("change", function () { control.disabled = clear.checked; dirty[key] = read(); });
          clearLabel.prepend(clear); editor.appendChild(clearLabel);
        }
      }
      control.dataset.field = key;
      if (kind === 'service_out_of_hours' && key === 'until') {
        var timeRead = read;
        read = function () { return timeRead() || null; };
      }
      control.addEventListener("change", function () { dirty[key] = read(); });
      control.addEventListener("input", function () { dirty[key] = read(); });
      label.appendChild(control); editor.appendChild(label);
    });
    editor.appendChild(button("Keep changes", function () {
      if (!Object.keys(dirty).length) { self.editing = false; self.paint(); return; }
      var sources = {};
      Object.keys(dirty).forEach(function (key) { sources[key] = { kind: "owner_said", reference: "Owner edited this control" }; });
      self.mutate(card, "/changes", { revision: card.revision, changes: [{ op: "set", id: line.id, fields: dirty, sources: sources }] });
    }));
    editor.appendChild(button("Cancel", function () { self.editing = false; self.load(); }));
    row.replaceChildren(editor); editor.querySelector("input,select,textarea").focus();
  };
  function consideredNotes(parent, card) {
    (card.considered || []).forEach(function (item) {
      var note = node("div", null, "bc-setup-considered");
      note.appendChild(node("strong", item.label + " · " + (item.state === "needs_answer" ? "Needs your answer" : "Left unchanged")));
      note.appendChild(node("p", "Assistant note; no setting changed."));
      if (item.state === "needs_answer") note.appendChild(node("p", item.question));
      parent.appendChild(note);
    });
  }
  Panel.prototype.history = function () {
    var self = this;
    this.msgs.querySelectorAll(".bc-setup-applied").forEach(function (el) { el.remove(); });
    this.data.cards.forEach(function (card) {
      var applied = card.lines.filter(function (line) { return line.status === "applied"; });
      var view = self.data.views[card.page];
      if (!view) return;
      var record = node("details", null, "bc-setup-applied");
      var stamp = applied.length ? new Date(applied[applied.length - 1].applied_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
      record.appendChild(node("summary", view.label + (applied.length ? " · applied " + stamp + " · " + applied.length + " lines" : " · setup record") + ((card.considered || []).length ? " · " + card.considered.length + " considered" : "")));
      var content = node("div"); record.appendChild(content);
      consideredNotes(content, card);
      applied.forEach(function (line) { content.appendChild(node("p", line.display)); });
      var before, loaded = false, more = button("Earlier records", async function () {
        more.disabled = true;
        try {
          var result = await request("/concierge/setup-cards/" + card.page + "/history" + (before ? "?before=" + before : ""));
          result.revisions.forEach(function (r) {
            if ((r.card.considered || []).length) {
              var notes = node("details"); notes.appendChild(node("summary", "Considered " + new Date(r.card.updated_at).toLocaleString()));
              consideredNotes(notes, r.card); content.appendChild(notes);
            }
            if (!r.card.last_action || r.card.last_action.kind !== "accept") return;
            var detail = node("details"); detail.appendChild(node("summary", "Applied " + new Date(r.card.updated_at).toLocaleString()));
            r.card.last_action.changes.forEach(function (change) { diff(detail, change); });
            content.appendChild(detail);
          });
          before = result.before; more.hidden = !before; loaded = true;
        } catch (err) { self.fail(err.message); } finally { more.disabled = false; }
      });
      content.appendChild(more);
      record.addEventListener("toggle", function () { if (record.open && !loaded) more.click(); });
      self.msgs.appendChild(record);
    });
  };
  // A browser warning supplements server-side revision checks; it is not a lock.
  var FORM_PATHS = {
    diary: ["/app/settings/scheduling"], intake: ["/app/diary/intake"],
    dependants: ["/app/diary/dependants"], invoices: ["/app/invoices/settings", "/app/payments/methods"],
    stages: ["/app/crm/settings"], fields: ["/app/crm/fields"], knowledge: ["/app/knowledge"]
  };
  var AFFECTED = {
    diary_settings: ["diary", "intake", "dependants", "invoices"],
    booking_details: ["diary", "intake", "fields"], dependants: ["diary", "dependants", "fields"],
    invoice_settings: ["invoices"], packages: ["invoices"],
    crm_settings: ["stages", "diary"], crm_fields: ["fields", "intake"], knowledge: ["knowledge"]
  };
  function formGroup(form) {
    if (!form || form.tagName !== "FORM" || form.method.toLowerCase() !== "post") return null;
    var url = new URL(form.action, window.location.href);
    if (url.origin !== window.location.origin) return null;
    return Object.keys(FORM_PATHS).find(function (group) {
      return FORM_PATHS[group].some(function (path) { return url.pathname === path || url.pathname.indexOf(path + "/") === 0; });
    });
  }
  function FormGuard(scope) {
    var self = this;
    this.scope = scope; this.prefix = "blam8_setup_forms:" + scope + ":";
    this.seen = {}; this.blocked = {}; this.started = performance.timeOrigin || Date.now();
    this.storage = false; this.channel = null;
    if (scope) {
      try { var probe = this.prefix + "probe"; localStorage.setItem(probe, "1"); localStorage.removeItem(probe); this.storage = true; } catch (_) {}
      try { if (window.BroadcastChannel) this.channel = new BroadcastChannel(this.prefix); } catch (_) {}
    }
    if (this.channel) this.channel.onmessage = function (event) { self.receive(event.data); };
    window.addEventListener("storage", function (event) {
      if (!event.key || event.key.indexOf(self.prefix) !== 0 || !event.newValue) return;
      try { self.receive(JSON.parse(event.newValue)); } catch (_) {}
    });
    this.check(true);
    if (!scope || (!this.storage && !this.channel)) Object.keys(FORM_PATHS).forEach(function (group) { self.blocked[group] = true; });
    window.addEventListener("submit", function (event) {
      self.check(false);
      var group = formGroup(event.target);
      if (!group || !self.blocked[group]) return;
      event.preventDefault(); event.stopImmediatePropagation(); self.decorate();
      var notice = event.target.querySelector("[data-setup-reload]");
      if (notice) notice.scrollIntoView({block: "nearest"});
    }, true);
    window.addEventListener("focus", function () { self.check(false); self.decorate(); });
    window.addEventListener("pageshow", function () { self.check(false); self.decorate(); });
    document.addEventListener("visibilitychange", function () { if (!document.hidden) { self.check(false); self.decorate(); } });
    this.observer = new MutationObserver(function () { self.decorate(); });
    this.observer.observe(document.body, {childList: true, subtree: true});
    this.decorate();
  }
  FormGuard.prototype.check = function (initial) {
    if (!this.storage) return;
    var self = this;
    Object.keys(AFFECTED).forEach(function (page) {
      try {
        var value = JSON.parse(localStorage.getItem(self.prefix + page) || "null");
        if (!value) return;
        if (!initial && self.seen[page] === value.token) return;
        self.seen[page] = value.token;
        if (!initial || value.at >= self.started) self.receive(value);
      } catch (_) {
        // A previously readable verification channel is now uncertain.
        Object.keys(FORM_PATHS).forEach(function (group) { self.blocked[group] = true; });
      }
    });
  };
  FormGuard.prototype.receive = function (value) {
    if (!value || value.scope !== this.scope || !Object.prototype.hasOwnProperty.call(AFFECTED, value.page)
        || typeof value.token !== "string" || typeof value.at !== "number") return;
    this.seen[value.page] = value.token;
    var self = this;
    AFFECTED[value.page].forEach(function (group) { self.blocked[group] = true; });
    this.decorate();
  };
  FormGuard.prototype.notify = function (card) {
    if (!card || !Object.prototype.hasOwnProperty.call(AFFECTED, card.page)) return;
    var value = {scope: this.scope, page: card.page, at: Date.now(), token: String(Date.now()) + ":" + Math.random()};
    this.receive(value);
    try { if (this.storage) localStorage.setItem(this.prefix + card.page, JSON.stringify(value)); } catch (_) {}
    try { if (this.channel) this.channel.postMessage(value); } catch (_) {}
  };
  FormGuard.prototype.decorate = function () {
    var self = this;
    document.querySelectorAll("form").forEach(function (form) {
      var group = formGroup(form);
      if (!group || !self.blocked[group] || form.querySelector("[data-setup-reload]")) return;
      var notice = node("div", "A setup change may have updated these settings. Reload before saving this form; reloading discards unsaved form edits. ", "bc-setup-error");
      notice.setAttribute("data-setup-reload", ""); notice.setAttribute("role", "status");
      notice.appendChild(button("Reload page", function () { window.location.reload(); }));
      form.prepend(notice);
    });
  };
  window.Blam8SetupCards = { Panel: Panel, request: request, FormGuard: FormGuard };
})();
