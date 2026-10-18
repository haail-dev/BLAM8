/* Discretion mode — mask anything identifying, on screen only.
 *
 * Injected by the middleware in src/app/main.py for tenants with
 * tenant_config.discretion on, and by nothing else. A tenant without it never
 * receives this file and there is no trace of it in their HTML.
 *
 * Why it exists: a real personal email address reached two published videos
 * because a phone screen-recording never passes through the recorder's
 * redaction map. Masking at the source means there is nothing to redact.
 *
 * Only TEXT NODES are touched. Input values, textarea contents and
 * contenteditable are left exactly as they are, so nothing typed or saved is
 * ever corrupted by the mask — you can film a screen and still trust the data
 * behind it.
 *
 * Every rule is idempotent: masked output can never match its own pattern
 * again, so the MutationObserver cannot loop.
 */
(function () {
  'use strict';

  var SKIP = { SCRIPT: 1, STYLE: 1, TEXTAREA: 1, INPUT: 1, SELECT: 1, NOSCRIPT: 1 };

  // Keep the first character and the domain: k***@gmail.com still reads as an
  // address on film, which matters — the point is a believable demo, not a
  // blacked-out one.
  var EMAIL = /\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})/g;

  // UK postcode: keep the outward half (LS9), mask the inward. The outward
  // half is a whole district, so it identifies nobody.
  var POSTCODE = /\b([A-Z]{1,2}[0-9][A-Z0-9]?)\s+([0-9][A-Z]{2})\b/g;

  // House number + street. The number is what identifies a house; the street
  // name is a whole road and stays, so the line still reads like an address.
  var STREET = /\b(\d{1,4}[A-Za-z]?)(\s+[A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+)*\s+(?:Road|Rd|Street|St|Lane|Ln|Avenue|Ave|Close|Drive|Dr|Way|Court|Crescent|Place|Terrace|Gardens|Grove|Hill|Park|Rise|View|Walk)\b)/g;

  // Deliberately narrow: a UK number starting 0 or +44, 10 digits or more.
  // Anything looser starts eating booking refs (BK-1042), invoice numbers and
  // money, which would make the mode useless to film with.
  var PHONE = /(\+44\s?|\b0)(\d[\d\s]{8,12}\d)\b/g;

  function mask(text) {
    if (text.indexOf('@') >= 0) {
      text = text.replace(EMAIL, function (_m, first, domain) {
        return first + '***' + domain;
      });
    }
    text = text.replace(POSTCODE, function (_m, outward) { return outward + ' ***'; });
    text = text.replace(STREET, function (_m, _num, rest) { return '***' + rest; });
    text = text.replace(PHONE, function (_m, prefix, rest) {
      var digits = rest.replace(/\s/g, '');
      return prefix + digits.slice(0, 3) + ' ***';
    });
    return text;
  }

  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) { apply(root); return; }
    if (root.nodeType !== 1) return;
    if (SKIP[root.tagName] || root.isContentEditable) return;
    var it = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        var p = n.parentNode;
        if (!p || SKIP[p.tagName] || p.isContentEditable) return NodeFilter.FILTER_REJECT;
        return NodeFilter.FILTER_ACCEPT;
      }
    });
    var n, batch = [];
    while ((n = it.nextNode())) batch.push(n);
    batch.forEach(apply);
  }

  function apply(node) {
    var before = node.nodeValue;
    if (!before || before.length < 6) return;
    var after = mask(before);
    if (after !== before) node.nodeValue = after;
  }

  function start() {
    walk(document.body);
    document.documentElement.setAttribute('data-discretion', 'on');
    new MutationObserver(function (records) {
      for (var i = 0; i < records.length; i++) {
        var r = records[i];
        if (r.type === 'characterData') { apply(r.target); continue; }
        for (var j = 0; j < r.addedNodes.length; j++) walk(r.addedNodes[j]);
      }
    }).observe(document.body, {
      childList: true, subtree: true, characterData: true
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', start);
  } else {
    start();
  }
})();
