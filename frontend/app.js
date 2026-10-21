const $ = (id) => document.getElementById(id);
const number = (value) => new Intl.NumberFormat('en-US').format(value);
let page = { offset: 0, limit: 50, total: 0, next_offset: null, users: [] };
let controller;
let initialization;
let debounce;
let ready = false;
let snapshotId;

async function api(path, signal) {
  const request = new AbortController();
  const abort = () => request.abort();
  signal?.addEventListener('abort', abort, { once: true });
  if (signal?.aborted) request.abort();
  const timeout = setTimeout(() => request.abort(), 15000);
  try {
    const response = await fetch(path, { signal: request.signal, cache: 'no-store' });
    const content = await response.json().catch(() => null);
    if (!response.ok || !content) {
      const error = new Error(content?.error || `API request failed (${response.status}). Please try again.`);
      error.status = response.status;
      throw error;
    }
    return content;
  } catch (error) {
    if (error.name === 'AbortError' && !signal?.aborted) throw new Error('The API request timed out. Please try again.');
    if (error instanceof TypeError) throw new Error('Cannot connect to the analysis API. Please try again.');
    throw error;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', abort);
  }
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showError(error) {
  $('error-message').textContent = error.message;
  $('error').hidden = false;
}

function tableMessage(text) {
  const cell = element('td', 'table-message', text);
  cell.colSpan = 4;
  const row = element('tr');
  row.append(cell);
  $('users').replaceChildren(row);
}

function disablePaging() {
  $('previous').disabled = $('next').disabled = $('export').disabled = true;
}

function clearStatistics(text) {
  ready = false;
  snapshotId = undefined;
  document.querySelector('.run-info').removeAttribute('data-snapshot-id');
  document.querySelector('.run-info').removeAttribute('data-environment');
  document.querySelector('.run-info').removeAttribute('data-job-run-id');
  $('search').disabled = $('sort').disabled = true;
  disablePaging();
  for (const id of ['total-users', 'unique-edges', 'duplicates', 'self-loops']) $(id).textContent = '—';
  $('required-users').replaceChildren(element('div', 'user-card', text));
  tableMessage(text);
  $('page-info').textContent = 'Waiting for data';
}

function showRequired(users) {
  $('required-users').replaceChildren(...users.map((user, index) => {
    const card = element('article', 'user-card');
    const title = element('div', 'user-title');
    title.append(element('span', 'avatar', String(index + 1).padStart(2, '0')),
      element('h3', '', user.user_id), element('span', 'tag', 'REQUIRED'));
    const counts = element('dl', 'card-counts');
    for (const [key, label] of [['followers', 'Followers'], ['followees', 'Followees']]) {
      const group = element('div');
      group.append(element('dt', '', label), element('dd', '', number(user[key])));
      counts.append(group);
    }
    card.append(title, counts,
      element('p', 'exact-stat', `${user.user_id} has ${user.followers} followers`),
      element('p', 'exact-stat', `${user.user_id} has ${user.followees} followees`));
    return card;
  }));
}

function renderPage() {
  if (!page.users.length) {
    const row = element('tr');
    const cell = element('td', 'table-message', 'No users match this ID prefix.');
    cell.colSpan = 4;
    row.append(cell);
    $('users').replaceChildren(row);
  } else {
    $('users').replaceChildren(...page.users.map((user) => {
      const row = element('tr');
      const statements = element('td', 'stat-lines');
      statements.append(element('div', '', `${user.user_id} has ${user.followers} followers`),
        element('div', '', `${user.user_id} has ${user.followees} followees`));
      row.append(element('td', 'user-id', user.user_id), element('td', 'number', number(user.followers)),
        element('td', 'number', number(user.followees)), statements);
      return row;
    }));
  }
  $('page-info').textContent = page.users.length ?
    `${number(page.offset + 1)}–${number(page.offset + page.users.length)} of ${number(page.total)} users` : '0 users';
  $('previous').disabled = page.offset === 0;
  $('next').disabled = page.next_offset === null;
  $('export').disabled = page.users.length === 0;
}

async function loadPage(offset = 0) {
  if (!ready) return;
  clearTimeout(debounce);
  controller?.abort();
  controller = new AbortController();
  const signal = controller.signal;
  disablePaging();
  const query = new URLSearchParams({ offset, limit: 50, sort: $('sort').value,
    search: $('search').value.trim(), snapshot_id: snapshotId });
  try {
    const nextPage = await api(`/api/users?${query}`, signal);
    if (signal.aborted) return;
    if (nextPage.snapshot_id !== snapshotId) {
      const error = new Error('Published statistics changed. Reload the analysis.');
      error.status = 412;
      throw error;
    }
    page = nextPage;
    renderPage();
    $('error').hidden = true;
  } catch (error) {
    if (!signal.aborted) {
      if (error.status === 412) {
        clearStatistics('Published statistics changed. Please try again.');
        $('run-label').textContent = 'Analysis needs refreshing';
      }
      showError(error);
      tableMessage('Unable to load statistics. Please try again.');
      $('page-info').textContent = 'Request failed';
    }
  }
}

async function initialize() {
  clearTimeout(debounce);
  controller?.abort();
  initialization?.abort();
  initialization = new AbortController();
  const signal = initialization.signal;
  clearStatistics('Loading user statistics…');
  $('run-label').textContent = 'Connecting to the analysis API…';
  $('error').hidden = true;
  try {
    const result = await api('/api/summary', signal);
    const required = await api(`/api/users?ids=214328887,107830991&snapshot_id=${result.snapshot_id}`, signal);
    if (signal.aborted) return;
    if (!result.snapshot_id || required.snapshot_id !== result.snapshot_id) throw new Error('Published statistics changed. Please try again.');
    if (required.missing_ids.length) throw new Error('A required user is missing from the published result.');
    for (const [id, key] of [['total-users', 'total_users'], ['unique-edges', 'unique_edges'],
      ['duplicates', 'duplicate_edges'], ['self-loops', 'self_loop_rows']]) {
      $(id).textContent = number(result.summary[key]);
    }
    const provenance = result.provenance;
    $('run-label').textContent = `${provenance.environment === 'cloud' ? 'Cloud run' : 'Local verification'} · ${provenance.engine}`;
    showRequired(required.users);
    snapshotId = result.snapshot_id;
    document.querySelector('.run-info').dataset.snapshotId = snapshotId;
    document.querySelector('.run-info').dataset.environment = provenance.environment;
    document.querySelector('.run-info').dataset.jobRunId = provenance.job_run_id || '';
    ready = true;
    $('search').disabled = $('sort').disabled = false;
    await loadPage();
  } catch (error) {
    if (signal.aborted) return;
    clearStatistics('Statistics unavailable. Please try again.');
    $('run-label').textContent = 'Analysis API unavailable';
    showError(error);
  }
}

$('filters').addEventListener('submit', (event) => { event.preventDefault(); clearTimeout(debounce); loadPage(); });
$('search').addEventListener('input', () => {
  clearTimeout(debounce);
  controller?.abort();
  disablePaging();
  debounce = setTimeout(() => loadPage(), 250);
});
$('sort').addEventListener('change', () => loadPage());
$('previous').addEventListener('click', () => loadPage(Math.max(0, page.offset - page.limit)));
$('next').addEventListener('click', () => loadPage(page.next_offset));
$('retry').addEventListener('click', initialize);
$('export').addEventListener('click', () => {
  const content = ['user_id,followers,followees', ...page.users.map((user) =>
    `${user.user_id},${user.followers},${user.followees}`)].join('\r\n');
  const url = URL.createObjectURL(new Blob([content], { type: 'text/csv;charset=utf-8' }));
  const link = element('a');
  link.href = url;
  link.download = `twitter-users-page-${page.offset + 1}.csv`;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
});
initialize();
