import { fileURLToPath } from 'node:url';
import { mkdir, readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';

export async function fileHash(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex');
}

export async function sourceHashes() {
  const paths = ['frontend/app.js', 'frontend/index.html', 'frontend/styles.css', 'backend/app.py', 'backend/web.py', 'backend/local_server.py',
    'scripts/verify_browser.mjs', 'scripts/capture_images.mjs'];
  return Object.fromEntries(await Promise.all(paths.map(async (name) =>
    [name, await fileHash(new URL(`../${name}`, import.meta.url))])));
}

export async function captureImages(page, browser, output, base) {
  const result = await (await page.request.get(`${base}/api/summary`)).json();
  if (await page.locator('.run-info').getAttribute('data-snapshot-id') !== result.snapshot_id) {
    throw new Error('Page capture and API use different published results');
  }
  if (await page.locator('.run-info').getAttribute('data-environment') !== result.provenance.environment ||
      await page.locator('.run-info').getAttribute('data-job-run-id') !== (result.provenance.job_run_id || '')) {
    throw new Error('Page capture and API have different execution provenance');
  }
  const assets = new URL('../report/assets/', import.meta.url);
  await mkdir(assets, { recursive: true });
  const sources = await sourceHashes();
  let manual;
  try {
    manual = JSON.parse(await readFile(new URL('manual-captures.json', output), 'utf8'));
  } catch {
    manual = null;
  }
  const appSources = ['frontend/app.js', 'frontend/index.html', 'frontend/styles.css', 'backend/app.py', 'backend/web.py'];
  let reviewed = manual?.review_status === 'REVIEWED' && manual.url === base &&
    manual.snapshot_id === result.snapshot_id &&
    isDeepStrictEqual(manual.provenance, result.provenance) &&
    appSources.every((name) => manual.source_sha256?.[name] === sources[name]);
  if (reviewed) {
    try {
      for (const name of ['manual-frontend.png', 'manual-api.png']) {
        if (manual.files?.[name] !== await fileHash(new URL(name, assets))) reviewed = false;
      }
    } catch {
      reviewed = false;
    }
  }
  const images = reviewed ? { frontend: 'manual-frontend.png', api: 'manual-api.png' } :
    { frontend: 'frontend-results.png', api: 'backend-api.png' };
  if (!reviewed) {
    const top = await page.locator('.run-info').boundingBox();
    const bottom = await page.locator('.required-section').boundingBox();
    await page.screenshot({
      path: fileURLToPath(new URL(images.frontend, assets)),
      clip: { x: top.x, y: top.y, width: top.width, height: bottom.y + bottom.height - top.y },
    });
    const apiPage = await browser.newPage({ viewport: { width: 850, height: 350 } });
    try {
      const response = await apiPage.goto(`${base}/api/users?ids=214328887,107830991&snapshot_id=${result.snapshot_id}`);
      if (!response.ok()) throw new Error('API result changed while taking screenshots');
      const pretty = apiPage.getByRole('checkbox');
      if (await pretty.count() === 1) await pretty.check();
      await apiPage.screenshot({ path: fileURLToPath(new URL(images.api, assets)) });
    } finally {
      await apiPage.close();
    }
  }
  return { url: base, snapshot_id: result.snapshot_id, provenance: result.provenance,
    captured_at: new Date().toISOString(), source_sha256: sources, images,
    files: Object.fromEntries(await Promise.all(Object.values(images).map(async (name) =>
      [name, await fileHash(new URL(name, assets))]))) };
}
