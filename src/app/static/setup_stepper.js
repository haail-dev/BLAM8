// Places the current step's name under its circle and tells the track where to stop
// (see setup_stepper.css). The name is clamped inside the stepper so a long one under
// the first or last circle never spills past the strip. Display only: nothing here
// moves the owner between steps.
(function () {
  var stepper = document.querySelector('[data-setup-stepper]');
  var label = stepper && stepper.querySelector('[data-setup-stepper-label]');
  var dot = stepper && stepper.querySelector('[data-setup-stepper-current] .setup-stepper-dot');
  if (!label || !dot) return;
  function layout() {
    var box = stepper.getBoundingClientRect(), circle = dot.getBoundingClientRect();
    var left = Math.max(0, Math.min(box.width - label.offsetWidth,
      circle.left - box.left + circle.width / 2 - label.offsetWidth / 2));
    label.style.setProperty('--label-left', left + 'px');
    stepper.style.setProperty('--label-start', left + 'px');
    stepper.style.setProperty('--label-end', (left + label.offsetWidth) + 'px');
  }
  window.addEventListener('resize', layout);
  if (window.ResizeObserver) new ResizeObserver(layout).observe(stepper);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(layout);
  layout();
})();
