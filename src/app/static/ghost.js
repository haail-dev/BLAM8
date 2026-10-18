/* Text-node reveal only: links, controls, countdowns and event order stay live. */
(function (root) {
  'use strict';
  function createTyper(doc, clock, motion) {
    var seen = new Map(), liveText = new Set(), active = [], timer = null;
    function paint(item) {
      var left = item.state.shown;
      item.nodes.forEach(function (part) {
        part.node.nodeValue = part.text.slice(0, left);
        left = Math.max(0, left - part.text.length);
      });
    }
    function tick() {
      timer = null;
      active = active.filter(function (item) {
        if (!item.el.isConnected) return false;
        var backlog = item.state.text.length - item.state.shown;
        // Long incoming lines reveal larger chunks so the display catches up.
        item.state.shown += motion.matches ? backlog : Math.min(backlog, Math.max(2, Math.ceil(backlog / 12)));
        paint(item);
        return item.state.shown < item.state.text.length;
      });
      if (active.length) timer = clock.setTimeout(tick, 24);
    }
    function finish() {
      active.forEach(function (item) { item.state.shown = item.state.text.length; paint(item); });
      active = [];
      if (timer !== null) clock.clearTimeout(timer);
      timer = null;
    }
    function reveal(el, key, live) {
      if (active.some(function (item) { return item.el === el; })) return;
      var nodes = [], walker = doc.createTreeWalker(el, 4), node;
      while ((node = walker.nextNode())) {
        if (node.parentElement.closest('button, [data-left], svg, [data-ghost-skip]')) continue;
        nodes.push({node: node, text: node.nodeValue});
      }
      var text = nodes.map(function (part) { return part.text; }).join('');
      var words = text.replace(/\s+/g, ' ').trim();
      if (live) liveText.add(words);
      var id = key + '\n' + text, state = seen.get(id);
      if (!state) {
        state = {text: text, shown: motion.matches ? text.length : Math.min(2, text.length)};
        if (!live && liveText.has(words)) state.shown = text.length;
        seen.forEach(function (previous, previousId) {
          if (previousId.startsWith(key + '\n') && text.startsWith(previous.text)) {
            state.shown = Math.max(state.shown, previous.shown);
          }
        });
        seen.set(id, state);
      }
      var item = {el: el, nodes: nodes, state: state};
      paint(item);
      if (state.shown < text.length) {
        active.push(item);
        if (timer === null) timer = clock.setTimeout(tick, 24);
      }
    }
    function scan(scope) {
      scope.querySelectorAll('[data-ghost-scope]').forEach(function (group) {
        // Diary controls remain immediate; only prose is animated.
        group.querySelectorAll('p').forEach(function (line) {
          if (!line.closest('[data-held-list]')) reveal(line, group.dataset.ghostScope);
        });
      });
    }
    if (motion.addEventListener) motion.addEventListener('change', function () { if (motion.matches) finish(); });
    return {reveal: reveal, scan: scan, finish: finish,
      begin: function () { finish(); liveText.clear(); }};
  }
  if (typeof module !== 'undefined' && module.exports) module.exports = createTyper;
  else root.blam8Ghost = createTyper(document, root, root.matchMedia('(prefers-reduced-motion: reduce)'));
})(typeof window === 'undefined' ? globalThis : window);
