import { chromium } from 'playwright-core';
import { mkdir, writeFile, readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { captureImages, sourceHashes } from './capture_images.mjs';

const executablePath = process.env.CHROME_PATH || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
const base = process.env.APP_URL || 'http://127.0.0.1:8000';
const output = new URL('../artifacts/evidence/', import.meta.url);
await mkdir(output, { recursive: true });
await writeFile(new URL('browser-verification.json', output), JSON.stringify({ status: 'RUNNING', url: base }));
let browser;
const checks = [];
try {
  const initialSources = await sourceHashes();
  browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1120 }, deviceScaleFactor: 1 });
  for (const name of ['index.html', 'styles.css', 'app.js']) {
    const response = await page.request.get(new URL(name, `${base.replace(/\/$/, '')}/`).href);
    assert.ok(response.ok(), `Cannot fetch deployed ${name}`);
    assert.equal(createHash('sha256').update(await response.body()).digest('hex'),
      initialSources[`frontend/${name}`], `Deployed ${name} differs from the verified local source`);
  }
  checks.push('Served frontend assets match the current source files');
  const errors = [];
  page.on('pageerror', (error) => errors.push(error.message));
  await page.goto(base);
  await page.getByText('214328887 has 180 followers', { exact: true }).first().waitFor();
  await page.getByText('214328887 has 214 followees', { exact: true }).first().waitFor();
  await page.getByText('107830991 has 180 followers', { exact: true }).first().waitFor();
  await page.getByText('107830991 has 157 followees', { exact: true }).first().waitFor();
  await page.getByText('1–50 of 81,306 users', { exact: true }).waitFor();
  assert.equal(await page.locator('#total-users').textContent(), '81,306');
  assert.equal(await page.locator('#users tr').count(), 50);
  checks.push('Required user counts and full dataset summary render from API');
  const capture = await captureImages(page, browser, output, base);
  assert.deepEqual(capture.source_sha256, initialSources, 'Source changed during screenshot capture');
  await page.locator('.run-info').evaluate((node) => { node.dataset.environment = 'mismatched-test'; });
  await assert.rejects(captureImages(page, browser, output, base), /execution provenance/);
  await page.locator('.run-info').evaluate((node, environment) => { node.dataset.environment = environment; }, capture.provenance.environment);
  checks.push('Screenshot capture rejects mismatched execution provenance');
  await page.getByRole('button', { name: 'Next →', exact: true }).click();
  await page.getByText('51–100 of 81,306 users', { exact: true }).waitFor();
  checks.push('Pagination fetches the next 50 users');
  await page.getByLabel('Search by user ID prefix').fill('107830991');
  await page.getByText('1–1 of 1 users', { exact: true }).waitFor();
  assert.equal(await page.locator('#users .user-id').textContent(), '107830991');
  checks.push('Search returns the requested user');
  await page.getByLabel('Sort by').selectOption('followers');
  await page.getByText('1–1 of 1 users', { exact: true }).waitFor();
  await page.getByLabel('Search by user ID prefix').fill('');
  await page.getByText('1–50 of 81,306 users', { exact: true }).waitFor();
  const degrees = await page.locator('#users tr td:nth-child(2)').allTextContents();
  const parsed = degrees.map((value) => Number(value.replaceAll(',', '')));
  assert.ok(parsed.every((value, index) => index === 0 || parsed[index - 1] >= value));
  checks.push('Follower sorting orders a complete page in descending order');
  await page.getByLabel('Search by user ID prefix').fill('99999999999999999999');
  await page.getByText('No users match this ID prefix.', { exact: true }).waitFor();
  checks.push('Empty search state renders correctly');
  await page.getByLabel('Search by user ID prefix').fill('invalid');
  await page.locator('#error').waitFor({ state: 'visible' });
  assert.match(await page.locator('#error-message').textContent(), /decimal/);
  checks.push('Invalid input displays the API error');
  await page.getByLabel('Search by user ID prefix').fill('214328887');
  await page.getByText('1–1 of 1 users', { exact: true }).waitFor();
  await page.locator('#error').waitFor({ state: 'hidden' });
  const downloadEvent = page.waitForEvent('download');
  await page.getByRole('button', { name: 'Download page CSV' }).click();
  const download = await downloadEvent;
  assert.equal(await readFile(await download.path(), 'utf8'),
    'user_id,followers,followees\r\n214328887,180,214');
  checks.push('CSV export downloads the displayed page');
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  checks.push('390px mobile layout has no page overflow');
  await page.route('**/api/users?*', (route) => route.fulfill({
    status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Test: statistics temporarily unavailable.' }),
  }));
  await page.getByLabel('Search by user ID prefix').fill('123');
  await page.locator('#error').waitFor({ state: 'visible' });
  await page.getByText('Unable to load statistics. Please try again.', { exact: true }).waitFor();
  checks.push('Backend failure is visible and stale rows are removed');
  await page.unroute('**/api/users?*');
  await page.getByRole('button', { name: 'Try again', exact: true }).click();
  await page.locator('#page-info').filter({ hasText: / users$/ }).waitFor();
  await page.locator('#error').waitFor({ state: 'hidden' });
  checks.push('Retry recovers after a failed directory request');

  const recovery = await browser.newPage();
  recovery.on('pageerror', (error) => errors.push(error.message));
  await recovery.route('**/api/summary', (route) => route.fulfill({
    status: 503, contentType: 'text/html', body: '<h1>Temporarily unavailable</h1>',
  }));
  await recovery.goto(base);
  await recovery.getByText('Statistics unavailable. Please try again.', { exact: true }).first().waitFor();
  assert.equal(await recovery.locator('#total-users').textContent(), '—');
  assert.equal(await recovery.locator('#search').isDisabled(), true);
  assert.equal(await recovery.getByText('Loading user statistics…', { exact: true }).count(), 0);
  assert.match(await recovery.locator('#error-message').textContent(), /503/);
  await recovery.unroute('**/api/summary');
  await recovery.getByRole('button', { name: 'Try again', exact: true }).click();
  await recovery.getByText('1–50 of 81,306 users', { exact: true }).waitFor();
  await recovery.locator('#error').waitFor({ state: 'hidden' });
  checks.push('Initial HTML error clears loading states; retry restores all statistics');

  await recovery.route('**/api/users?*', async (route) => {
    const search = new URL(route.request().url()).searchParams.get('search');
    const response = await route.fetch();
    if (search === '107830991') await new Promise((resolve) => setTimeout(resolve, 600));
    await route.fulfill({ response });
  });
  await recovery.getByLabel('Search by user ID prefix').fill('107830991');
  await recovery.waitForRequest((request) => new URL(request.url()).searchParams.get('search') === '107830991');
  await recovery.getByLabel('Search by user ID prefix').fill('214328887');
  await recovery.getByText('1–1 of 1 users', { exact: true }).waitFor();
  await recovery.waitForTimeout(800);
  assert.equal(await recovery.locator('#users .user-id').textContent(), '214328887');
  checks.push('A delayed older search cannot overwrite the latest search');
  await recovery.unroute('**/api/users?*');
  await recovery.route('**/api/users?*', (route) => route.fulfill({
    status: 412, contentType: 'application/json', body: JSON.stringify({ error: 'Published statistics changed. Reload the analysis.' }),
  }));
  await recovery.getByLabel('Search by user ID prefix').fill('123');
  await recovery.locator('#error').waitFor({ state: 'visible' });
  assert.equal(await recovery.locator('#total-users').textContent(), '—');
  assert.equal(await recovery.locator('#search').isDisabled(), true);
  checks.push('Result version changes clear stale statistics and require refresh');
  await recovery.unroute('**/api/users?*');
  await recovery.getByRole('button', { name: 'Try again', exact: true }).click();
  await recovery.getByLabel('Search by user ID prefix').fill('');
  await recovery.getByText('1–50 of 81,306 users', { exact: true }).waitFor();
  await recovery.route('**/api/users?*', async (route) => {
    const response = await route.fetch();
    await route.fulfill({ response, json: { ...await response.json(), snapshot_id: 'changed-result' } });
  });
  await recovery.getByLabel('Search by user ID prefix').fill('123');
  await recovery.locator('#error').waitFor({ state: 'visible' });
  assert.equal(await recovery.locator('#total-users').textContent(), '—');
  assert.equal(await recovery.locator('#search').isDisabled(), true);
  checks.push('An inconsistent successful response also clears the previous result');
  await recovery.close();
  assert.deepEqual(errors, []);
  checks.push('No uncaught browser JavaScript errors');
  assert.deepEqual(await sourceHashes(), initialSources, 'Source changed during browser verification');
  const result = { status: 'PASS', environment: capture.provenance.environment, url: base,
    snapshot_id: capture.snapshot_id, provenance: capture.provenance,
    verified_at: new Date().toISOString(), source_sha256: initialSources,
    captures: { captured_at: capture.captured_at, images: capture.images, files: capture.files }, checks };
  await writeFile(new URL('browser-verification.json', output), `${JSON.stringify(result, null, 2)}\n`);
  console.log(JSON.stringify(result, null, 2));
} catch (error) {
  await writeFile(new URL('browser-verification.json', output), `${JSON.stringify({ status: 'FAIL', url: base, error: error.message }, null, 2)}\n`);
  throw error;
} finally {
  await browser?.close();
}
