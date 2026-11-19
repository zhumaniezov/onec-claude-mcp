import { build } from 'esbuild';
import { readFile, writeFile, readdir, stat } from 'node:fs/promises';
import path from 'node:path';

const bundle = await build({
  entryPoints: ['src/server.mjs'], outfile: 'dist/server.cjs', bundle: true,
  platform: 'node', format: 'cjs', target: 'node22', metafile: true,
  legalComments: 'eof', logLevel: 'warning'
});

// Include the license texts of packages whose files are part of the offline distribution.
const packages = new Set();
for (const input of Object.keys(bundle.metafile.inputs)) {
  const match = /^(.*node_modules\/(?:@[^/]+\/)?[^/]+)/.exec(input);
  if (match) packages.add(match[1]);
}
const notices = [];
for (const directory of [...packages].sort()) {
  const pkg = JSON.parse(await readFile(path.join(directory, 'package.json'), 'utf8'));
  const files = (await readdir(directory)).filter(n => /^(licen[sc]e|copying|notice)([.-].*)?$/i.test(n));
  const licenseTexts = [];
  for (const file of files) {
    const full = path.join(directory, file);
    if ((await stat(full)).isFile()) licenseTexts.push(await readFile(full, 'utf8'));
  }
  if (!licenseTexts.length && pkg.name === 'saxes' && pkg.version === '6.0.0') {
    licenseTexts.push(await readFile('scripts/saxes-6.0.0.LICENSE.txt', 'utf8'));
  }
  if (!licenseTexts.length) throw new Error('Отсутствует файл лицензии: ' + pkg.name);
  notices.push(pkg.name + ' ' + pkg.version + '\n' + licenseTexts.join('\n'));
}
await writeFile('dist/THIRD-PARTY-NOTICES.txt', notices.join('\n\n--------------------------------\n\n'));
