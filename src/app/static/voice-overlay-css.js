// Shared presentation only; no voice engine or media acquisition.
export const OVERLAY_CSS = `
#voice-live-root { display: none; }
#voice-live-root.is-open {
  display: block; position: fixed; inset: 0; z-index: 90;
  background: radial-gradient(120% 120% at 50% 0%, #16233f 0%, #0b1220 60%, #070d18 100%);
  color: #e2e8f0; font-family: inherit;
}
#voice-live-root [hidden] { display: none !important; }
#voice-live-root, #voice-live-root * { box-sizing: border-box; }
#voice-live-root .vl-stage { position: absolute; inset: 0; display: flex; flex-direction: column; }
#voice-live-root .vl-toasts {
  position: absolute; top: max(0.9rem, env(safe-area-inset-top)); left: 0; right: 0;
  display: flex; flex-direction: column; align-items: center; gap: 0.4rem;
  pointer-events: none; z-index: 3;
}
#voice-live-root .vl-toast {
  max-width: 85%; border-radius: 9999px; background: rgba(226,232,240,0.14);
  backdrop-filter: blur(6px); padding: 0.4rem 0.9rem; font-size: 0.8rem;
  animation: vl-toast 4.5s ease forwards;
}
@keyframes vl-toast {
  0% { opacity: 0; transform: translateY(-6px); }
  8%, 82% { opacity: 1; transform: translateY(0); }
  100% { opacity: 0; transform: translateY(-6px); }
}
/* One screen (donor b662c74, 2026-08-29): the orb column, with the newest
   reply's cards in a sheet sharing it. No pages, no swipe, no dots. */
#voice-live-root .vl-live {
  flex: 1 1 auto; min-height: 0; width: 100%; position: relative;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  /* The top clears the notch with room to spare. */
  gap: 1rem; padding: calc(max(0.9rem, env(safe-area-inset-top)) + 1.25rem) 1.5rem 1rem;
}
/* The ripples (docs/spec-voice-ripples.md): a canvas behind the orb, the
   whole voice screen, never in the way of a tap. */
#voice-live-root .vl-ripples { position: absolute; inset: 0; z-index: 0; pointer-events: none; }
#voice-live-root .vl-live > :not(.vl-ripples) { position: relative; z-index: 1; }
#voice-live-root .vl-orb {
  width: 10.5rem; height: 10.5rem; flex-shrink: 0; border-radius: 9999px; position: relative;
  background: radial-gradient(circle at 32% 30%, #7dd3fc 0%, #2563eb 55%, #1e3a8a 100%);
  box-shadow: 0 0 60px 6px rgba(37, 99, 235, 0.35);
  transition: width 0.3s ease, height 0.3s ease, transform 0.2s ease, box-shadow 0.3s ease, filter 0.3s ease;
}
/* The ring's colours are variables so a ripple can tint them (below): the
   glow is registered so it slides like the border does. */
@property --vl-glow { syntax: "<color>"; inherits: true; initial-value: rgb(34 197 94); }
#voice-live-root { --vl-ring: rgba(134, 239, 172, 0.9); --vl-glow: rgb(34 197 94); }
#voice-live-root .vl-orb span {
  position: absolute; inset: -0.55rem; border-radius: 9999px;
  border: 2px solid var(--vl-ring);
  transform: scale(calc(1 + var(--vl-level, 0) * 0.22));
  transition: transform 0.09s linear, border-color 1.2s ease, --vl-glow 1.2s ease;
  /* The ring wears the mic's colour and pulse (Kris 2026-09-23). */
  animation: vl-ring-live 1.8s ease-in-out infinite;
}
/* A ripple's birth (Kris 2026-09-30): the ring slides into the wave's colour
   quickly, holds while the crest leaves, then eases back over the slow
   transition above once the class goes. */
#voice-live-root.vl-ring-tint .vl-orb span { transition: transform 0.09s linear, border-color 0.3s ease, --vl-glow 0.3s ease; }
#voice-live-root[data-phase="connecting"] .vl-orb { filter: saturate(0.4) brightness(0.8); }
#voice-live-root[data-phase="thinking"] .vl-orb { animation: vl-think 1.5s ease-in-out infinite; }
#voice-live-root[data-phase="speaking"] .vl-orb { box-shadow: 0 0 80px 14px rgba(125, 211, 252, 0.45); }
#voice-live-root[data-phase="error"] .vl-orb { background: radial-gradient(circle at 32% 30%, #fda4af 0%, #e11d48 55%, #881337 100%); box-shadow: 0 0 50px 4px rgba(225, 29, 72, 0.3); }
@keyframes vl-think {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(0.94); }
}
/* The drop (spec §3 timeline): the mic dips 4% over 120 ms as the stone hits,
   the orb shudders once. With reduced motion, one gentle brightness pulse on
   the drop and on ready instead of any particles. */
#voice-live-root.vl-drop .vl-mic { animation: vl-mic-dip 240ms ease-out; }
#voice-live-root.vl-drop .vl-orb { animation: vl-orb-shudder 260ms ease-out; }
@keyframes vl-mic-dip {
  0%, 100% { transform: translate(-50%, -50%) scale(1); }
  50% { transform: translate(-50%, -50%) scale(0.96); }
}
@keyframes vl-orb-shudder {
  0%, 100% { transform: scale(1); }
  45% { transform: scale(0.985); }
}
@keyframes vl-glow-pulse {
  0%, 100% { filter: brightness(1); }
  40% { filter: brightness(1.28); }
}
@media (prefers-reduced-motion: reduce) {
  #voice-live-root.vl-drop .vl-mic, #voice-live-root.vl-drop .vl-orb { animation: none; }
  #voice-live-root.vl-pulse .vl-orb { animation: vl-glow-pulse 700ms ease-out; }
}
#voice-live-root .vl-status { font-size: 0.95rem; color: #94a3b8; min-height: 1.4rem; text-align: center; }
#voice-live-root .vl-debug { font-size: 0.7rem; color: #64748b; font-variant-numeric: tabular-nums; }
#voice-live-root .vl-transcript, #voice-live-root .vl-reply { display:none; }
#voice-live-root .vl-transcript {
  min-height: 1.5rem; max-width: 34rem; text-align: center;
  font-size: 1.05rem; color: #e2e8f0;
}
/* What was heard IS the cancel (Kris 2026-09-23: "make the text clickable to
   cancel on click"). From the first words until the answer is finished the
   line is tappable — a dashed underline says so; tapped, it's struck. */
#voice-live-root .vl-transcript.is-strikable {
  cursor: pointer; border-radius: 0.5rem; padding: 0.15rem 0.4rem;
  text-decoration: underline dashed rgba(148, 163, 184, 0.7);
  text-underline-offset: 0.3em; -webkit-tap-highlight-color: transparent;
}
#voice-live-root .vl-transcript.is-strikable:hover,
#voice-live-root .vl-transcript.is-strikable:active {
  background: rgba(248, 113, 113, 0.16); text-decoration-color: rgba(248, 113, 113, 0.9);
}
/* Heard, held, not sent (2026-09-24): the words stay with "tap to send" under
   them — never gone without a word. */
#voice-live-root .vl-transcript.is-held {
  cursor: pointer; border-radius: 0.5rem; padding: 0.15rem 0.5rem; color: #cbd5e1;
  border: 1px dashed rgba(245, 158, 11, 0.7); -webkit-tap-highlight-color: transparent;
}
#voice-live-root .vl-transcript.is-held::after {
  content: "Tap to send"; display: block; margin-top: 0.2rem;
  font-size: 0.72rem; font-weight: 600; color: #fcd34d;
}
/* Tour: started from the Concierge's settings cog; the card explains the screen. */
#voice-live-root .vl-tour {
  position: absolute; left: 50%; bottom: max(5rem, calc(env(safe-area-inset-bottom) + 4rem));
  transform: translateX(-50%); z-index: 5; width: min(24rem, calc(100% - 2rem));
  border-radius: 1rem; padding: 1rem 1.1rem 0.9rem;
  background: rgba(15, 23, 42, 0.96); border: 1px solid rgba(148, 163, 184, 0.35);
  box-shadow: 0 18px 40px rgba(0, 0, 0, 0.45); text-align: left; white-space: normal;
}
#voice-live-root .vl-tour h3 { margin: 0 0 0.35rem; font-size: 1rem; color: #f8fafc; font-weight: 600; }
#voice-live-root .vl-tour p { margin: 0; font-size: 0.9rem; line-height: 1.5; color: #cbd5e1; }
#voice-live-root .vl-tour-foot { display: flex; align-items: center; justify-content: space-between; margin-top: 0.9rem; }
#voice-live-root .vl-tour-step { font-size: 0.75rem; color: #94a3b8; }
#voice-live-root .vl-tour-foot button {
  margin-left: 0.4rem; height: 2.1rem; padding: 0 0.95rem; border-radius: 9999px; cursor: pointer;
  border: 1px solid rgba(148, 163, 184, 0.45); background: transparent; color: #e2e8f0; font-size: 0.85rem;
}
#voice-live-root .vl-tour-foot button.is-primary { background: #f8fafc; color: #0b1220; border-color: #f8fafc; }
/* While a step talks about a control, that control glows. */
#voice-live-root .vl-tour-spot { outline: 2px solid rgba(125, 211, 252, 0.9); outline-offset: 6px; border-radius: 9999px; }
/* The reply is the thing being read (Kris 2026-09-23: "small and has to be
   scrolled"): readable size, and it takes whatever height the orb column has
   left, scrolling only past that. */
#voice-live-root .vl-reply {
  min-height: 1.4rem; max-width: 34rem; flex: 0 1 auto; overflow-y: auto;
  overscroll-behavior: contain;
  text-align: center; font-size: 1.05rem; line-height: 1.5; color: #cbd5e1;
}
/* The cards: no sheet of their own any more (Kris 2026-09-23: "surrounded by
   a big white box"). They sit low, just above End conversation, on the dark
   screen; links are glass pills, and a Confirm card keeps its own light card
   because it holds a draft to read and edit. */
/* The bottom holds the card whole — it doesn't scroll (Kris 2026-09-24: "the
   whole card & suggested nav button is in a scrollable container, that's
   wrong"). Only an email's text box scrolls, inside the card. The done deck
   keeps its own scrolling stage (its rule in _concierge_panel.html). */
#voice-live-root .vl-stage-scroll {
  flex: none; width: min(35rem, calc(100% - 2rem)); margin: 0 auto 0.6rem;
  font-size: 0.875rem; text-align: left;
}
/* A card fits the bottom third (Kris 2026-09-24: "the draft container is too
   big for our bottom third"): about four lines of the email show, the rest
   scrolls inside its box; padding and gaps are tighter than in the chat. */
#voice-live-root .vl-stage-scroll textarea {
  height: 12vh; min-height: 4.5rem; max-height: 12vh;
  overflow-y: auto; overscroll-behavior: contain; resize: none;
}
#voice-live-root .vl-stage-scroll .rounded-2xl { padding: 0.6rem 0.8rem 0.7rem; max-width: 100%; }
#voice-live-root .vl-stage-scroll .rounded-2xl.space-y-2 > * + * { margin-top: 0.35rem; }
#voice-live-root .vl-stage-scroll .cz-draft { padding: 0.55rem 0.75rem 0.5rem; }
#voice-live-root .vl-stage-scroll .cz-draft-text { margin-top: 0.35rem; }
#voice-live-root .vl-stage-scroll:empty { display: none; }
#voice-live-root .vl-stage-scroll > div > * + * { margin-top: 0.6rem; }
#voice-live-root .vl-stage-scroll > div > div:first-child { margin-top: 0; justify-content: center; }
#voice-live-root .vl-stage-scroll a {
  background: rgba(226, 232, 240, 0.1); color: #e2e8f0;
  border: 1px solid rgba(148, 163, 184, 0.35); border-radius: 9999px;
  padding: 0.5rem 1rem; font-size: 0.85rem; text-decoration: none;
}
#voice-live-root .vl-stage-scroll a:hover { background: rgba(226, 232, 240, 0.18); }
/* With cards on screen the orb cedes the room: smaller, still animating, so
   the reply gets the height instead. */
#voice-live-root.has-cards .vl-orb { width: 5.5rem; height: 5.5rem; }
/* Centred in the room the cards leave (Kris 2026-09-23: "too pushed up" —
   flex-start jammed the orb against the notch). Auto margins, not
   justify-content: center, so a long reply overflowing still starts at the
   top instead of being cut off above it. */
#voice-live-root.has-cards .vl-live { justify-content: flex-start; }
#voice-live-root.has-cards .vl-live > :first-child { margin-top: auto; }
#voice-live-root.has-cards .vl-live > :last-child { margin-bottom: auto; }
/* The mic sits in the middle of the orb (Kris 2026-09-23): a plain mic with
   a pulsing green glow while live; tapped, a struck-through mic with a
   pulsing red glow. The rest of the orb is still the tap-to-interrupt. */
/* Low, with only a little room under it (Kris 2026-09-24: "more space for
   the stuff in the bottom third"): it may sit partly into the home
   indicator's inset — ~16px on an iPhone, 8px where there is none. */
#voice-live-root .vl-controls {
  flex: none; display: flex; justify-content: center; flex-wrap:wrap; gap:.5rem;
  margin-bottom: max(0.5rem, calc(env(safe-area-inset-bottom) - 1.1rem));
}
#voice-live-root .vl-controls button { min-height:44px; padding:.55rem .85rem; border:1px solid #64748b; border-radius:9999px; background:transparent; color:#cbd5e1; }
#voice-live-root .vl-mic {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
  width: 4.5rem; height: 4.5rem; padding: 0; margin: 0;
  display: flex; align-items: center; justify-content: center;
  border: 0; background: transparent; cursor: pointer; z-index: 1;
  color: #86efac;
  -webkit-tap-highlight-color: transparent;
}
/* No circle of its own: the icon's outline IS the glow. The button stays a
   full-size invisible tap target around it. */
#voice-live-root .vl-mic svg {
  width: 2.75rem; height: 2.75rem; overflow: visible; stroke-width: 2.4;
  animation: vl-mic-live 1.8s ease-in-out infinite;
}
#voice-live-root .vl-mic[aria-pressed="true"] { color: #fca5a5; }
#voice-live-root .vl-mic[aria-pressed="true"] svg { animation-name: vl-mic-muted; }
#voice-live-root.has-cards .vl-mic { width: 3rem; height: 3rem; }
#voice-live-root.has-cards .vl-mic svg { width: 1.8rem; height: 1.8rem; }
@keyframes vl-mic-live {
  0%, 100% { filter: drop-shadow(0 0 1px rgba(34, 197, 94, 0.9)); }
  50% { filter: drop-shadow(0 0 6px rgba(34, 197, 94, 1)) drop-shadow(0 0 12px rgba(34, 197, 94, 0.7)); }
}
@keyframes vl-mic-muted {
  0%, 100% { filter: drop-shadow(0 0 1px rgba(239, 68, 68, 0.9)); }
  50% { filter: drop-shadow(0 0 6px rgba(239, 68, 68, 1)) drop-shadow(0 0 12px rgba(239, 68, 68, 0.7)); }
}
/* Muted: the ring turns red with the mic and stops following the level. */
#voice-live-root.is-muted .vl-orb span {
  transform: scale(1); border-color: rgba(252, 165, 165, 0.95);
  animation-name: vl-ring-muted;
}
@keyframes vl-ring-live {
  0%, 100% { box-shadow: 0 0 2px color-mix(in srgb, var(--vl-glow) 60%, transparent); }
  50% { box-shadow: 0 0 10px 2px color-mix(in srgb, var(--vl-glow) 90%, transparent), 0 0 22px 4px color-mix(in srgb, var(--vl-glow) 45%, transparent); }
}
@keyframes vl-ring-muted {
  0%, 100% { box-shadow: 0 0 2px rgba(239, 68, 68, 0.6); }
  50% { box-shadow: 0 0 10px 2px rgba(239, 68, 68, 0.9), 0 0 22px 4px rgba(239, 68, 68, 0.45); }
}
#voice-live-root .vl-end {
  flex: none; margin: 0; display: inline-flex; align-items: center; height: 2.4rem; line-height: 1;
  border: 1px solid rgba(148, 163, 184, 0.4); border-radius: 9999px;
  background: transparent; color: #cbd5e1; font-size: 0.85rem;
  padding: 0.55rem 1.4rem; cursor: pointer;
}
#voice-live-root .vl-end:hover { background: rgba(148, 163, 184, 0.12); }
body.voice-live-open { overflow: hidden; }
/* Pull down on the orb column to refresh (Kris 2026-09-23: "add the pull down
   to refresh to the VC"): src/app/static/pull_refresh.js does the pull; the
   reload lands on #voice and the Concierge reopens ready to talk. Its label
   sits under the overlay's z-index, so it lifts while voice is open. */
#voice-live-root #pull-refresh-label { z-index: 95; color: #94a3b8; }
#voice-live-root[data-phase="paused"] .vl-orb { filter: saturate(0.5) brightness(0.85); }
`;
