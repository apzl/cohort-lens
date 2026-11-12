// Render the hero loop animation in docs/index.html to docs/investigation-loop.gif.
// The animation replays docs/sample-run/gdsc-lineage-enriched-v1.ledger.jsonl, which the page embeds
// inline; regenerate the GIF after changing that ledger or the diagram.
//
// The page exposes a seekable timeline in capture mode (docs/index.html?capture=1):
//   window.__duration  loop length in ms
//   window.__seek(ms)  draw the diagram at that time
//
// Dependencies are NOT vendored in this repo. Install them anywhere and point NODE_PATH at them:
//   mkdir -p /tmp/cohortlens-gif && cd /tmp/cohortlens-gif && npm init -y && npm i playwright-core gifenc pngjs
//   npx playwright-core install chromium        # skip if ~/.cache/ms-playwright already has chromium
//   NODE_PATH=/tmp/cohortlens-gif/node_modules node tools/render_pipeline_gif.mjs
//
// Options (env): FPS (default 10), OUT (default docs/investigation-loop.gif), CHROMIUM (browser executable),
//                FRAMES_DIR (also write a few PNG frames there for inspection).
import { createRequire } from 'node:module';
import { fileURLToPath, pathToFileURL } from 'node:url';
import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..');
const nodePath = (process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean);
const require = createRequire(nodePath.length ? path.join(nodePath[0], 'x.js') : import.meta.url);
const { chromium } = require('playwright-core');
const { GIFEncoder, quantize, applyPalette } = require('gifenc');
const { PNG } = require('pngjs');

const W = 1200, H = 620;
const FPS = Number(process.env.FPS || 10);
const OUT = path.resolve(repo, process.env.OUT || 'docs/investigation-loop.gif');
const FRAMES_DIR = process.env.FRAMES_DIR;

function findChromium() {
  if (process.env.CHROMIUM) return process.env.CHROMIUM;
  const base = path.join(os.homedir(), '.cache', 'ms-playwright');
  if (!fs.existsSync(base)) return undefined;
  const dirs = fs.readdirSync(base).sort().reverse();
  for (const d of dirs) {
    for (const rel of ['chrome-headless-shell-linux64/chrome-headless-shell', 'chrome-linux64/chrome', 'chrome-linux/chrome']) {
      const p = path.join(base, d, rel);
      if (fs.existsSync(p)) return p;
    }
  }
  return undefined;
}

const browser = await chromium.launch({ executablePath: findChromium() });
const page = await browser.newPage({ viewport: { width: W, height: H }, deviceScaleFactor: 1 });
const errors = [];
page.on('pageerror', e => errors.push(String(e)));
page.on('console', m => { if (m.type() === 'error') errors.push(m.text()); });
await page.goto(pathToFileURL(path.join(repo, 'docs/index.html')).href + '?capture=1');
await page.waitForFunction('window.__ready === true');
await page.evaluate(() => document.fonts.ready);
const duration = await page.evaluate('window.__duration');
const n = Math.round(duration / 1000 * FPS);
const delay = Math.round(1000 / FPS);

const frames = [];
for (let i = 0; i < n; i++) {
  await page.evaluate(ms => window.__seek(ms), i * duration / n);
  const png = PNG.sync.read(await page.screenshot({ type: 'png' }));
  frames.push(png.data);
  if (FRAMES_DIR && i % Math.floor(n / 6) === 2) {
    fs.mkdirSync(FRAMES_DIR, { recursive: true });
    fs.writeFileSync(path.join(FRAMES_DIR, `frame-${String(i).padStart(3, '0')}.png`), PNG.sync.write(png));
  }
}
await browser.close();
if (errors.length) { console.error('Page errors:\n' + errors.join('\n')); process.exit(1); }

// One global palette (255 colours + 1 transparent slot) sampled across the loop.
const sampleIdx = [...Array(12).keys()].map(k => Math.floor(k * n / 12));
const sample = new Uint8Array(sampleIdx.length * W * H * 4);
sampleIdx.forEach((fi, k) => sample.set(frames[fi], k * W * H * 4));
const palette = quantize(sample, 255, { format: 'rgb565' });
const TRANSPARENT = palette.length;           // index 255
palette.push([0, 0, 0]);

// Frame differencing: pixels unchanged since the previous frame become transparent.
const gif = GIFEncoder();
let prev = null;
for (let i = 0; i < n; i++) {
  const idx = applyPalette(frames[i], palette.slice(0, TRANSPARENT), 'rgb565');
  let out = idx;
  if (prev) {
    out = new Uint8Array(idx.length);
    for (let p = 0; p < idx.length; p++) out[p] = idx[p] === prev[p] ? TRANSPARENT : idx[p];
  }
  gif.writeFrame(out, W, H, {
    palette: i === 0 ? palette : undefined,
    delay, repeat: 0, dispose: 1,
    transparent: i > 0, transparentIndex: TRANSPARENT,
  });
  prev = idx;
}
gif.finish();
fs.writeFileSync(OUT, gif.bytes());
console.log(`${path.relative(repo, OUT)}: ${W}x${H}, ${n} frames @ ${FPS} fps, ${(duration / 1000).toFixed(1)} s loop, ${(fs.statSync(OUT).size / 1024).toFixed(0)} KB`);
