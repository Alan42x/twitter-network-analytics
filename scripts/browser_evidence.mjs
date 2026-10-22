import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';

export async function fileHash(path) {
  return createHash('sha256').update(await readFile(path)).digest('hex');
}

export async function sourceHashes() {
  const paths = ['frontend/app.js', 'frontend/index.html', 'frontend/styles.css', 'backend/app.py', 'backend/web.py', 'backend/local_server.py',
    'scripts/verify_browser.mjs', 'scripts/browser_evidence.mjs'];
  return Object.fromEntries(await Promise.all(paths.map(async (name) =>
    [name, await fileHash(new URL(`../${name}`, import.meta.url))])));
}

export async function reviewedCaptures(page, output, base) {
  const result = await (await page.request.get(`${base}/api/summary`)).json();
  if (await page.locator('.run-info').getAttribute('data-snapshot-id') !== result.snapshot_id) {
    throw new Error('Page capture and API use different published results');
  }
  if (await page.locator('.run-info').getAttribute('data-environment') !== result.provenance.environment ||
      await page.locator('.run-info').getAttribute('data-job-run-id') !== (result.provenance.job_run_id || '')) {
    throw new Error('Page capture and API have different execution provenance');
  }
  const assets = new URL('../report/assets/', import.meta.url);
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
  // Runtime screenshots are supplied by the user. Verification never creates or replaces them.
  const images = reviewed ? { frontend: 'manual-frontend.png', api: 'manual-api.png' } : {};
  return { url: base, snapshot_id: result.snapshot_id, provenance: result.provenance,
    status: reviewed ? 'REVIEWED' : 'AWAITING_MANUAL_SCREENSHOTS',
    captured_at: reviewed ? manual.reviewed_at : null, source_sha256: sources, images,
    files: Object.fromEntries(await Promise.all(Object.values(images).map(async (name) =>
      [name, await fileHash(new URL(name, assets))]))) };
}
