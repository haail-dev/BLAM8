/* Deploy-time Tailwind build for the PUBLIC site shell (base.html and every
 * page extending it) → src/app/static/tw-site.css. This is base.html's former
 * Play-CDN inline config, ported verbatim (security review 2026-09, M10: the
 * CDN ran third-party script on /login and sent every visitor's IP away).
 * Counting House (2026-08-29): ink text on warm paper, hairline borders, one
 * cobalt accent. The slate scale is remapped wholesale so every existing
 * utility warms up without template edits; blue-600/700 fold onto the accent
 * so old bg-blue buttons match bg-brand. */
module.exports = {
  content: ['src/app/templates/**/*.html', 'src/app/static/**/*.js', 'src/app/**/*.py'],
  theme: {
    extend: {
      fontFamily: {
        sans: ['Inter', 'system-ui', '-apple-system', 'sans-serif'],
        display: ['"Schibsted Grotesk"', 'Inter', 'system-ui', 'sans-serif'],
      },
      borderRadius: { lg: '0.375rem', xl: '0.625rem', '2xl': '0.75rem' },
      colors: {
        navy: { DEFAULT: '#101A26', 900: '#0B121C', 800: '#1F2C3B', 700: '#2B3A4C' },
        brand: { DEFAULT: '#1D4FD7', 600: '#1D4FD7', 500: '#3E68DF', 50: '#EFF2FB' },
        slate: { 50:'#F7F6F3',100:'#F2F0EC',200:'#E5E2DC',300:'#D6D2CA',400:'#8493A5',
                 500:'#5C6B7D',600:'#46586C',700:'#2B3A4C',800:'#1F2C3B',900:'#101A26' },
        blue: { 50:'#EFF2FB',100:'#DEE6F8',200:'#BFCEF2',300:'#93ACE8',400:'#6386DE',
                500:'#3E68DF',600:'#1D4FD7',700:'#173FB0',800:'#133490',900:'#102A73' },
      },
    },
  },
};
