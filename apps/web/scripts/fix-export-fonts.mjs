import { readdir, readFile, stat, writeFile } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';

const clientDir = resolve('dist/client');
const assetExtensions = new Set(['.html', '.css', '.js']);
const absolutePathPattern =
  /(?:^|["'(=\s])(?:file:\/\/\/|\/(?:Users|home|private|tmp|var|opt|etc|System|Volumes)\/|[A-Za-z]:[\\/])/;
const fontSourcePattern = /\/[^"'\s<>]*\/\.vinext\/fonts\//g;
const fontUrlPattern = /\/_next\/static\/_vinext_fonts\/[^"'()\s]+/g;

async function filesIn(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...(await filesIn(path)));
    else if (assetExtensions.has(path.slice(path.lastIndexOf('.'))))
      files.push(path);
  }
  return files;
}

const files = await filesIn(clientDir);
const fontUrls = new Set();
for (const path of files) {
  const original = await readFile(path, 'utf8');
  const rewritten = original.replace(
    fontSourcePattern,
    '/_next/static/_vinext_fonts/',
  );
  if (rewritten !== original) await writeFile(path, rewritten);
  if (absolutePathPattern.test(rewritten))
    throw new Error(
      `absolute filesystem path remains in ${relative(clientDir, path)}`,
    );
  for (const match of rewritten.matchAll(fontUrlPattern)) {
    const url = match[0].split(/[?#]/, 1)[0];
    if (/\.(?:woff2?|ttf|otf)$/.test(url)) fontUrls.add(url);
  }
}

for (const url of fontUrls) {
  const file = join(clientDir, url.slice(1));
  const info = await stat(file).catch(() => null);
  if (!info?.isFile())
    throw new Error(`font URL does not map to a file: ${url}`);
}

console.log(`fixed ${fontUrls.size} exported font URLs`);
