/* Deploy-time Tailwind build for the APP shells (app / admin / onboarding —
 * every template that includes _theme_head.html).
 *
 * This is the former _theme_head.html runtime config, ported verbatim: the
 * Play CDN (cdn.tailwindcss.com) ran this token math in the browser and
 * recompiled every page's CSS on every navigation — real seconds on a phone
 * (Kris 2026-08-31). scripts/build/build_css.sh runs it once per deploy instead,
 * emitting src/app/static/tw.css. Marketing/capture/portal/track keep their own
 * inline-config CDN setup — different palette, different shell.
 *
 * The theme-variable blocks and the Counting House FLAT layer are emitted by
 * the plugin below so everything lives in this one file: addBase for the vars
 * and element rules, addUtilities for the shadow-flattening overrides (plugin
 * utilities land AFTER core utilities, preserving the old
 * appended-stylesheet-wins ordering).
 */

const plugin = require('tailwindcss/plugin');

// Tailwind v3 hexes for every hue the templates use (status chips etc.).
const HUES = {
  blue:    { 50:'#eff6ff',100:'#dbeafe',200:'#bfdbfe',300:'#93c5fd',400:'#60a5fa',500:'#3b82f6',600:'#2563eb',700:'#1d4ed8',800:'#1e40af',900:'#1e3a8a' },
  emerald: { 50:'#ecfdf5',100:'#d1fae5',200:'#a7f3d0',300:'#6ee7b7',400:'#34d399',500:'#10b981',600:'#059669',700:'#047857',800:'#065f46',900:'#064e3b' },
  amber:   { 50:'#fffbeb',100:'#fef3c7',200:'#fde68a',300:'#fcd34d',400:'#fbbf24',500:'#f59e0b',600:'#d97706',700:'#b45309',800:'#92400e',900:'#78350f' },
  rose:    { 50:'#fff1f2',100:'#ffe4e6',200:'#fecdd3',300:'#fda4af',400:'#fb7185',500:'#f43f5e',600:'#e11d48',700:'#be123c',800:'#9f1239',900:'#881337' },
  red:     { 50:'#fef2f2',100:'#fee2e2',200:'#fecaca',300:'#fca5a5',400:'#f87171',500:'#ef4444',600:'#dc2626',700:'#b91c1c',800:'#991b1b',900:'#7f1d1d' },
  sky:     { 50:'#f0f9ff',100:'#e0f2fe',200:'#bae6fd',300:'#7dd3fc',400:'#38bdf8',500:'#0ea5e9',600:'#0284c7',700:'#0369a1',800:'#075985',900:'#0c4a6e' },
  indigo:  { 50:'#eef2ff',100:'#e0e7ff',200:'#c7d2fe',300:'#a5b4fc',400:'#818cf8',500:'#6366f1',600:'#4f46e5',700:'#4338ca',800:'#3730a3',900:'#312e81' },
  violet:  { 50:'#f5f3ff',100:'#ede9fe',200:'#ddd6fe',300:'#c4b5fd',400:'#a78bfa',500:'#8b5cf6',600:'#7c3aed',700:'#6d28d9',800:'#5b21b6',900:'#4c1d95' },
  teal:    { 50:'#f0fdfa',100:'#ccfbf1',200:'#99f6e4',300:'#5eead4',400:'#2dd4bf',500:'#14b8a6',600:'#0d9488',700:'#0f766e',800:'#115e59',900:'#134e4a' },
  green:   { 50:'#f0fdf4',100:'#dcfce7',200:'#bbf7d0',300:'#86efac',400:'#4ade80',500:'#22c55e',600:'#16a34a',700:'#15803d',800:'#166534',900:'#14532d' }
};

// Counting House ink scale (replaces blue-tinted slate) + warm paper.
const INK = { 400:'#8494A9', 500:'#5A6A80', 600:'#3E4C61', 700:'#273244', 800:'#17202E', 900:'#0B1220' };
const PAPER = { 0:'#ffffff', 50:'#F6F8FA', 100:'#EFF2F6', 200:'#E4E9EF' };
const LINE = { 50:'#EDF0F4', 100:'#E9EDF2', 200:'#DEE4EB', 300:'#CBD4DF', 400:'#AEBAC9' };
const ACCENT = '#2E5BFF', ACCENT_INK = '#2246D6', ACCENT_WASH = '#EDF2FF';

// Dark surfaces: the same ink, as grounds (slightly warm, not slate-blue).
const PAGE = '#12161D', PANEL = '#1A202A';

function hexRgb(h) { h = h.slice(1); return [parseInt(h.slice(0,2),16), parseInt(h.slice(2,4),16), parseInt(h.slice(4,6),16)]; }
function mix(fg, bg, t) { // t = how much fg over bg
  const a = hexRgb(fg), b = hexRgb(bg), o = [];
  for (let i = 0; i < 3; i++) o[i] = Math.round(a[i] * t + b[i] * (1 - t));
  return '#' + o.map((v) => v.toString(16).padStart(2, '0')).join('');
}
function trip(h) { return hexRgb(h).join(' '); }

// T[plugin][utility-token] = [lightHex, darkHex]
const T = { bg: {}, tx: {}, bd: {}, rg: {}, ph: {}, gs: {} };
function tok(g, n, l, d) { T[g][n] = [l, d]; }

// --- neutrals (surfaces + text + borders) ---
tok('bg', 'white',     PAPER[0],  PANEL);
tok('bg', 'slate-50',  PAPER[50], '#222933');   // inset / hover on cards
tok('bg', 'slate-100', PAPER[100], PAGE);       // page bg (+ deep hover)
tok('bg', 'slate-200', PAPER[200], '#2B333F');
tok('bg', 'slate-800', INK[800],  '#0C1117');   // code blocks go deeper
tok('bg', 'slate-900', INK[900],  '#080C12');
tok('bg', 'navy',      INK[900],  INK[900]);    // ONE navy in both modes — bar, strip and notch cap match always
tok('bg', 'navy-800',  INK[800],  '#202836');
tok('bg', 'brand-50',  ACCENT_WASH, mix(ACCENT, PANEL, 0.16));
tok('bg', 'blue-700',  ACCENT_INK, mix(ACCENT, PANEL, 0.75)); // hover pair of bg-brand
tok('tx', 'navy',      INK[900],  '#EAE9E4');
tok('tx', 'slate-800', INK[800],  '#E2E1DC');
tok('tx', 'slate-700', INK[700],  '#C9C9C4');
tok('tx', 'slate-600', INK[600],  '#A9ABA8');
tok('tx', 'slate-500', INK[500],  '#8C9092');
tok('tx', 'slate-400', INK[400],  '#71787D');
tok('tx', 'brand',     ACCENT,    '#7E9EF2');
// Text you TYPE, as opposed to text you read (Kris 2026-09-01: the settings
// textareas read as black). Its own token because the shared slate ladder is
// too coarse here — the rung below was invisible, the one below that was too
// far, and the two modes need different sized steps.
//
// Landed by being wrong twice: slate-600 changed light too little to notice,
// then INK[500] changed it too much ("too much change" on a light PC) while
// leaving dark exactly where it started ("no change on mobile at all" — the
// phone is in dark mode, and I had frozen that value deliberately). So both
// values move now, and neither goes the whole way:
//   light #4C5971  7.06:1 on white          (was 12.92 at slate-700)
//   dark  #BBBCB7  5.86:1 on the dark field (was 6.74)
// Both clear of AA, and softened by a similar amount so the two themes agree
// about how much quieter a typing box should be than the page around it.
tok('tx', 'field',     '#4C5971', '#BBBCB7');
tok('bd', 'slate-50',  LINE[50],  '#232A34');
tok('bd', 'slate-100', LINE[100], '#272E39');
tok('bd', 'slate-200', LINE[200], '#2E3641');
tok('bd', 'slate-300', LINE[300], '#3A4450');
tok('bd', 'slate-400', LINE[400], '#485466');
tok('bd', 'brand',     ACCENT,    '#4A6BD1');
tok('rg', 'brand',     ACCENT,    '#4A6BD1');
tok('ph', 'slate-500', INK[500],  '#6E7680');
tok('gs', 'slate-50',  PAPER[50], '#222933');

// --- status hues: light tints become tint-on-panel blends, deep text goes light ---
Object.keys(HUES).forEach((h) => {
  const P = HUES[h];
  tok('bg', h + '-50',  P[50],  mix(P[500], PANEL, 0.14));
  tok('bg', h + '-100', P[100], mix(P[500], PANEL, 0.22));
  tok('bg', h + '-200', P[200], mix(P[500], PANEL, 0.30));
  tok('tx', h + '-600', P[600], P[400]);
  tok('tx', h + '-700', P[700], P[300]);
  tok('tx', h + '-800', P[800], P[300]);
  tok('tx', h + '-900', P[900], P[200]);
  tok('bd', h + '-100', P[100], mix(P[500], PANEL, 0.30));
  tok('bd', h + '-200', P[200], mix(P[500], PANEL, 0.38));
  tok('bd', h + '-300', P[300], mix(P[500], PANEL, 0.48));
});
// saturated mid/deep shades (bg-emerald-600 buttons, bg-rose-500 dots, text-*-300/400,
// ring-emerald-400, border-*-400+ ...) are deliberately unmapped: stock Tailwind, both modes.
// blue-700 bg IS mapped above: it's the hover pair of bg-brand and stock #1d4ed8
// sits a hair from the new accent, which would make hover a no-op.

// The :root (light) + dark variable blocks (R G B triplets so /alpha works).
const lightVars = {}, darkVars = {};
Object.keys(T).forEach((g) => {
  Object.keys(T[g]).forEach((n) => {
    const v = '--c-' + g + '-' + n;
    lightVars[v] = trip(T[g][n][0]);
    darkVars[v] = trip(T[g][n][1]);
  });
});

// Per-plugin Tailwind maps: 'slate-50' -> slate:{50:...}, 'navy' -> navy:{DEFAULT:...}
function build(g) {
  const out = {};
  Object.keys(T[g]).forEach((n) => {
    const v = 'rgb(var(--c-' + g + '-' + n + ')/<alpha-value>)';
    const m = n.match(/^(.*)-([0-9]+)$/);
    if (n === 'white') { out.white = v; return; }
    const fam = m ? m[1] : n, shade = m ? m[2] : 'DEFAULT';
    out[fam] = out[fam] || {};
    out[fam][shade] = v;
  });
  return out;
}

module.exports = {
  content: [
    'src/app/templates/**/*.html',
    'src/app/static/**/*.js',
    'src/app/**/*.py',            // a few fragment endpoints build class strings in Python
  ],
  theme: { extend: {
    fontFamily: {
      sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
      display: ['"Space Grotesk"', 'Inter', 'system-ui', 'sans-serif']
    },
    // Counting House radius scale: controls 6px, cards/popovers 10px, 12px max
    borderRadius: { lg: '0.375rem', xl: '0.625rem', '2xl': '0.75rem' },
    // legacy direct colours (gradients etc. that aren't per-plugin mapped)
    colors: { navy: { DEFAULT: '#101A26', 800: '#1F2C3B' }, brand: { DEFAULT: '#1D4FD7', 50: '#EFF2FB' } },
    backgroundColor: build('bg'),
    textColor: build('tx'),
    borderColor: build('bd'),        // divide-* inherits borderColor in Tailwind v3
    ringColor: build('rg'),
    placeholderColor: build('ph'),
    gradientColorStops: build('gs')
  } },
  plugins: [
    plugin(function ({ addBase, addUtilities }) {
      addBase({
        ':root': lightVars,
        'html[data-theme="dark"]': { ...darkVars, colorScheme: 'dark' },
        'html[data-theme="dark"] ::selection': { background: 'rgba(126,158,242,.35)' },
        // --- Counting House flat layer, zero template edits ---
        // invisible scrollbars, app-wide (Julius 07-31: "people know how to
        // scroll"); scrolling itself is untouched.
        '*': { 'scrollbar-width': 'none', '-ms-overflow-style': 'none' },
        '*::-webkit-scrollbar': { width: '0', height: '0', display: 'none' },
        // display face on headings; NO tnum anywhere (it widens hyphens/periods).
        'h1,h2,h3,.font-display': {
          fontFamily: '"Space Grotesk",Inter,system-ui,sans-serif',
          letterSpacing: '-0.015em'
        },
        // burger dropdowns: solid paper over the page — the ONE shadowed surface
        '#menu,#admin-menu': { boxShadow: '0 8px 24px rgba(16,26,38,.12),0 2px 6px rgba(16,26,38,.06)' },
        'html[data-theme="dark"] #menu,html[data-theme="dark"] #admin-menu': {
          boxShadow: '0 8px 24px rgba(0,0,0,.5),0 2px 6px rgba(0,0,0,.35)'
        },
      });
      // legacy shadow utilities on cards flatten to the hairline look — as
      // plugin UTILITIES so they land after core utilities and win the tie,
      // exactly like the old appended stylesheet did.
      addUtilities({
        '.shadow,.shadow-sm,.shadow-md,.shadow-lg': { boxShadow: 'none' },
      });
    }),
  ],
};
