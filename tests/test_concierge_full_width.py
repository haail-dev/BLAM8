"""Concierge on a phone: the tinted backdrop that the first message starts runs from screen edge to screen edge,
the newest section runs on down behind the composer to the foot of the screen, and the links under the composer
sit close to the foot (Kris 2026-10-16).

Real layout needs a browser; this pins the rules that make it so, in the one panel both frames share.
Run: python -m pytest tests/test_concierge_full_width.py -q
"""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
PANEL = (ROOT / "src/app/templates/app/_concierge_panel.html").read_text(encoding="utf-8")
JS = (ROOT / "src/app/static/concierge_panel.js").read_text(encoding="utf-8")
CSS = (ROOT / "src/app/static/concierge_discovery.css").read_text(encoding="utf-8")


def _phone_block():
    m = re.search(r"@media \(max-width:767px\) \{(.*?)\n\}", PANEL, re.S)
    assert m, "the phone block for the full-width backdrop is missing"
    return m.group(1)


def test_backdrop_pulls_out_over_the_panels_side_padding_on_a_phone():
    block = _phone_block()
    # the scroller must be as wide as the screen, or it clips a section that pulls out past it
    assert "#cz-root #cz-messages {margin-inline:-1rem;padding-inline:1rem;}" in block
    assert "#cz-root .cz-mode-section {margin-inline:-1rem;}" in block
    # 1rem is the panel's own px-4: the pull-out and the padding it undoes must stay one number
    assert 'id="cz-root" class="relative h-full flex flex-col max-w-3xl mx-auto w-full px-4 md:px-6' in PANEL


def test_full_width_is_phone_only_and_leaves_the_items_inset_alone():
    # no rule outside the phone block widens the section or the scroller (computers keep the panel's width)
    outside = PANEL.replace(_phone_block(), "")
    assert "margin-inline:-1rem" not in outside
    # the bubbles keep their own inset from the section edge, so they move out with it
    assert "#cz-root .cz-mode-item {position:relative;isolation:isolate;padding:.45rem .5rem;" in PANEL


def test_links_keep_their_tap_height_with_the_text_low_in_it():
    rule = re.search(r"@media \(max-width:480px\) \{ \.cz-conversation-actions #cz-explore, "
                     r"\.cz-conversation-actions #cz-clear \{([^}]*)\}", CSS)
    assert rule, "the phone rule for the two links is missing"
    assert "min-height:28px" in rule.group(1) and "padding-top:6px" in rule.group(1)


def test_newest_section_fills_the_chat_and_its_gradient_carries_on_behind_the_composer():
    block = _phone_block()
    # armed by the script only when it can measure the footer; the section fills the chat area to its foot
    assert "#cz-root[data-cz-tail] #cz-messages {display:flex;flex-direction:column;}" in block
    assert "#cz-root[data-cz-tail] #cz-messages > .cz-mode-last {flex:1 0 auto;margin-bottom:-1rem;padding-bottom:1rem;}" in block
    # the chat area still ends above the composer: it is NOT stretched over the footer, so nothing in the thread passes behind the links
    assert "margin-bottom:calc(-1 *" not in block and "scroll-padding-bottom" not in block
    # the tint behind the footer is a layer on the panel: the next stretch of the section's own gradient (same angle, same wash)
    assert "#cz-root[data-cz-tail][data-cz-tail-mode]::after {" in block
    assert "background:linear-gradient(105deg,var(--cz-tail-wash),transparent 88%)" in block
    assert "background-size:100% calc(var(--cz-last-h,0px) + var(--cz-foot-h,0px));background-position:0 calc(-1 * var(--cz-last-h,0px));}" in block
    for mode, wash in (("text", "37,99,235,.075"), ("live", "124,58,237,.075"), ("vc", "8,145,178,.075"), ("unknown", "100,116,139,.06")):
        assert '#cz-root[data-cz-tail-mode="%s"] {--cz-tail-wash:rgba(%s);}' % (mode, wash) in block
        assert wash in PANEL.split("@media (max-width:767px)")[0]      # the same wash the sections themselves use
    # the composer, links and waiting chips sit above that layer
    assert "#cz-root[data-cz-tail] > #cz-form" in block and "z-index:1;}" in block
    # computers keep the section where it ends
    assert "cz-mode-last" not in PANEL.replace(block, "")


def test_the_script_marks_the_last_section_and_reads_the_footer_height():
    # the last tinted section is marked only when nothing but the wait dot follows it (a card after it ends the tint as before)
    assert "classList.toggle('cz-mode-last', flush && sec === newestSection)" in JS
    assert "if (after !== waitDotRow && !after.hidden) { flush = false; break; }" in JS
    # one number CSS cannot know: the footer's height, kept current by a ResizeObserver; the CSS is armed only when it exists
    assert "function fitFoot()" in JS and "root.style.setProperty('--cz-foot-h'" in JS
    assert JS.index("var footObserver = new ResizeObserver(fitFoot);") < JS.index("root.setAttribute('data-cz-tail', '');")
    assert JS.count("function fitFoot()") == 1       # inside mount(): both frames use it
    assert JS.index("function mount(root)") < JS.index("function fitFoot()")
    # the layer behind the footer continues the newest section's own gradient: its mode picks the colour, its height places it
    assert "setTail(flush ? newestSection : null);" in JS
    assert "root.style.setProperty('--cz-last-h', tailSection.offsetHeight + 'px');" in JS
    assert "root.setAttribute('data-cz-tail-mode', sec.getAttribute('data-cz-mode') || 'unknown')" in JS


def test_chat_scrolls_up_to_the_bar_and_the_gap_above_the_first_section_is_small():
    block = _phone_block()
    outside = PANEL.replace(block, "")
    # the panel's 1rem top padding moves into the scroller, at every width: scrolled text reaches the bar
    assert "#cz-root #cz-messages {margin-top:-1rem;padding-top:1rem;}" in outside
    # the first section sits about 0.2rem under the bar (1rem - 0.8rem), where it used to sit about 1.8rem under it
    assert "#cz-root #cz-messages > .cz-mode-section:not(* ~ *) {margin-top:-.8rem;}" in outside
    # only the gap changed: no override of the first section's hairline or corners (Kris 2026-10-16)
    assert "border-top" not in block and "border-radius" not in block
    assert "{margin-top:-.5rem;border" not in outside


def test_observed_speech_text_is_not_repeated_on_every_message():
    history = (ROOT / "src/app/templates/app/_concierge_history.html").read_text(encoding="utf-8")
    assert "Observed speech" not in history and "Observed speech" not in JS
    # the section's tag (Text / Live / VC) is the one label, on the message that opens the section
    assert "label.hidden = !begins;" in JS
    assert "data-cz-observation" in history                  # the marker itself stays on the row


def test_the_chat_fades_into_the_background_where_it_meets_the_composer():
    # nothing of its own colour is laid on top (a white overlay did not match the page's gradient): the chat's lower edge is masked out
    assert "#cz-root[data-cz-tail]::before" not in PANEL and "rgb(255 255 255" not in PANEL and "cz-more-below" not in PANEL
    assert "#cz-root[data-cz-tail] #cz-messages {-webkit-mask-image:linear-gradient(to bottom,#000 calc(100% - var(--cz-edge,0px)),transparent);" in PANEL
    assert "mask-image:linear-gradient(to bottom,#000 calc(100% - var(--cz-edge,0px)),transparent);}" in PANEL
    # the tint behind the composer eases away with it, so both meet the same background at the edge
    assert "#cz-root[data-cz-tail][data-cz-tail-mode]::after {opacity:var(--cz-tail-o,1);}" in PANEL
    # the script sets how much of the chat lies below the edge (0 to 56px): on scroll, on a resize, after every repaint of the sections
    assert "Math.max(0, Math.min(56, Math.round(messages.scrollHeight - messages.scrollTop - messages.clientHeight)))" in JS
    assert "root.style.setProperty('--cz-edge', hidden + 'px');" in JS
    assert "root.style.setProperty('--cz-tail-o', String(Math.round((1 - hidden / 56) * 100) / 100));" in JS
    assert "messages.addEventListener('scroll', fitEdge, { passive: true });" in JS
    assert "new ResizeObserver(fitEdge).observe(messages);" in JS
    assert "setTail(flush ? newestSection : null);\n      fitEdge();" in JS
