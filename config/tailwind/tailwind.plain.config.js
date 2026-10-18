/* Deploy-time Tailwind build for the small standalone pages — admin sign-in,
 * /track, /capture, /partner, the driver page → src/app/static/tw-plain.css.
 * Stock Tailwind plus the navy/brand those pages' former Play-CDN inline
 * configs added (security review 2026-09, M10). */
module.exports = {
  content: ['src/app/templates/**/*.html', 'src/app/static/**/*.js', 'src/app/**/*.py'],
  theme: {
    extend: {
      fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
      colors: { navy: { DEFAULT: '#0b1b34', 800: '#102544' }, brand: { DEFAULT: '#2563eb' } },
    },
  },
};
