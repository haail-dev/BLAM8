// Portable local equivalent of the pinned standalone compiler used on Render.
import { execFileSync } from 'node:child_process';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const root = fileURLToPath(new URL('../../', import.meta.url));
const compiler = require.resolve('tailwindcss/lib/cli.js');
for (const [config, output] of [
  ['config/tailwind/tailwind.config.js', 'tw.css'],
  ['config/tailwind/tailwind.site.config.js', 'tw-site.css'],
  ['config/tailwind/tailwind.plain.config.js', 'tw-plain.css'],
]) {
  execFileSync(process.execPath, [compiler, '--config', config,
    '--input', 'src/app/static/src/tw-input.css', '--output', `src/app/static/${output}`, '--minify'],
  { cwd: root, stdio: 'inherit' });
}
