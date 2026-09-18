import test from 'node:test';
import assert from 'node:assert/strict';
import { access, readFile, mkdtemp, mkdir, rm, cp } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import http from 'node:http';
import { spawnSync } from 'node:child_process';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const appPath = path.join(root, 'src', 'app.js');
async function app() {
  assert.ok(await access(appPath).then(() => true, () => false), 'Phase 1 Dashboard app.js must exist');
  return import(appPath);
}

test('health label distinguishes fake degraded and unavailable states', async () => {
  const { healthLabel } = await app();
  assert.equal(healthLabel({ ready: true, status: 'healthy' }), '运行正常');
  assert.equal(healthLabel({ ready: true, status: 'degraded' }), '部分机器人离线');
  assert.equal(healthLabel({ ready: false, status: 'unavailable' }), '当前没有可用机器人');
});

test('same-origin API preserves robot-specific payload and reports validation errors', async () => {
  const { requestJSON } = await app();
  const observed = [];
  const server = http.createServer(async (req, res) => {
    let body = '';
    for await (const chunk of req) body += chunk;
    observed.push([req.url, req.method, body]);
    res.setHeader('content-type', 'application/json');
    if (req.url === '/health') {
      res.statusCode = 503;
      res.end(JSON.stringify({ ready: false, robots: [] }));
    } else if (body.includes('invalid')) {
      res.statusCode = 422;
      res.end(JSON.stringify({ detail: [{ msg: 'Text must not be blank' }] }));
    } else {
      res.end(JSON.stringify({ robot_id: 'annie-b', status: 'completed', reply: '<script>plain text</script>' }));
    }
  });
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  try {
    const result = await requestJSON(`${url}/api/robots/annie-b/turns`, { method: 'POST', body: { text: 'hello' } });
    assert.equal(result.robot_id, 'annie-b');
    assert.equal(result.reply, '<script>plain text</script>');
    assert.deepEqual(observed[0], ['/api/robots/annie-b/turns', 'POST', '{"text":"hello"}']);
    assert.deepEqual(await requestJSON(`${url}/health`, {}, [503]), { ready: false, robots: [] });
    await assert.rejects(requestJSON(`${url}/api/robots/annie-a/turns`, { method: 'POST', body: { text: 'invalid' } }), /422.*Text must not be blank/);
  } finally {
    await new Promise((resolve) => server.close(resolve));
  }
});

test('dashboard markup is accessible and loads only local assets', async () => {
  const html = await readFile(path.join(root, 'src', 'index.html'), 'utf8').catch(() => '');
  assert.match(html, /Annie Harbor/);
  assert.match(html, /lang="zh-CN"/);
  assert.match(html, /role="status"/);
  assert.match(html, /type="module" src="\/app.js"/);
  assert.match(html, /Fake/);
  assert.match(html, /一个进程/);
  assert.match(html, /多个机器人头并发/);
  assert.match(html, /\/phase-tests\/phase1\.html/);
  assert.match(html, /\/phase-tests\/phase2\.html/);
  assert.doesNotMatch(html, /两个独立的 Fake RobotRuntime/);
  assert.doesNotMatch(html, /https?:\/\//);
  const js = await readFile(appPath, 'utf8');
  assert.doesNotMatch(js, /\.innerHTML\s*=/);
  for (const action of ['cancel', 'stop', 'start', 'fail']) assert.match(js, new RegExp(`['"]${action}['"]`));
  assert.match(await readFile(path.join(root, 'src', 'styles.css'), 'utf8'), /@media/);
});

test('dependency-free build works after copying web to an independent directory', async () => {
  const sourcePackage = await readFile(path.join(root, 'package.json'), 'utf8').catch(() => '{}');
  const pkg = JSON.parse(sourcePackage);
  assert.equal(pkg.private, true);
  assert.equal(pkg.type, 'module');
  assert.deepEqual(pkg.dependencies ?? {}, {});
  const temporary = await mkdtemp(path.join(root, 'tests', '.build-copy-'));
  try {
    const copyRoot = path.join(temporary, 'independent-web');
    await mkdir(copyRoot);
    for (const name of ['src', 'scripts', 'package.json', 'README.md']) {
      await cp(path.join(root, name), path.join(copyRoot, name), { recursive: true });
    }
    const result = spawnSync(process.execPath, [path.join(copyRoot, 'scripts', 'build.mjs')], { cwd: temporary, encoding: 'utf8' });
    assert.equal(result.status, 0, result.stderr);
    assert.match(result.stdout, /Built Annie Harbor Dashboard/);
    for (const name of ['index.html', 'app.js', 'styles.css']) {
      assert.equal(await readFile(path.join(copyRoot, 'dist', name), 'utf8'), await readFile(path.join(copyRoot, 'src', name), 'utf8'));
    }
    const syntax = spawnSync(process.execPath, ['--check', path.join(copyRoot, 'dist', 'app.js')], { encoding: 'utf8' });
    assert.equal(syntax.status, 0, syntax.stderr);
  } finally {
    await rm(temporary, { recursive: true, force: true });
  }
});

class DOMNode {
  constructor() { this.dataset = {}; this.textContent = ''; this.hidden = false; this.children = []; this.listeners = new Map(); this.classList = { toggle() {} }; }
  append(node) { this.children.push(node); }
  replaceChildren() { this.children = []; }
  setAttribute() {}
  addEventListener(name, callback) { this.listeners.set(name, callback); }
}

async function dashboardHarness(robot) {
  let requests = 0;
  const health = { ready: true, status: 'healthy', pid: 123, robots: robot ? [robot] : [], capabilities: { brain: 'fake' } };
  const nodes = new Map(['robots', 'connection-error', 'health-status', 'health-dot', 'process-id', 'robot-count', 'capability-list'].map((name) => [`#${name}`, new DOMNode()]));
  nodes.set('#robot-template', { content: { firstElementChild: { cloneNode() {
    const card = new DOMNode();
    const selectors = ['h3', '.robot-avatar', 'input', 'label', 'form', '.robot-room', '.robot-state', '.completed', '.cancelled', '.failures', '.queue-depth', '.last-reply', '.runtime-error', '.send-button', '.action-message', '.last-input', '.current-request'];
    const childNodes = new Map(selectors.map((selector) => [selector, new DOMNode()]));
    for (const name of ['start', 'stop', 'cancel', 'fail']) {
      const button = new DOMNode();
      button.dataset.action = name;
      childNodes.set(`[data-action="${name}"]`, button);
    }
    card.querySelector = (selector) => childNodes.get(selector);
    card.querySelectorAll = () => ['start', 'stop', 'cancel', 'fail'].map((name) => childNodes.get(`[data-action="${name}"]`));
    return card;
  } } } });
  const saved = { document: globalThis.document, window: globalThis.window, fetch: globalThis.fetch, setTimeout: globalThis.setTimeout, clearTimeout: globalThis.clearTimeout };
  const timers = new Map();
  let nextTimer = 0;
  globalThis.document = { querySelector: (selector) => nodes.get(selector), createElement: () => new DOMNode() };
  globalThis.window = new EventTarget();
  globalThis.fetch = async () => {
    requests += 1;
    return new Response(JSON.stringify(health), { headers: { 'content-type': 'application/json' } });
  };
  globalThis.setTimeout = (callback) => { const id = ++nextTimer; timers.set(id, callback); return id; };
  globalThis.clearTimeout = (id) => timers.delete(id);
  const pause = () => new Promise((resolve) => saved.setTimeout(resolve, 10));
  return {
    nodes, timers, window: globalThis.window,
    requests: () => requests,
    async waitFor(predicate, message) { for (let i = 0; i < 60 && !predicate(); i += 1) await pause(); assert.ok(predicate(), message); },
    async load() { await import(`${appPath}?dashboard-test=${Math.random()}`); },
    async close() {
      globalThis.window.dispatchEvent(new Event('pagehide'));
      Object.assign(globalThis, saved);
    },
  };
}

function restoredPage() {
  const event = new Event('pageshow');
  Object.defineProperty(event, 'persisted', { value: true });
  return event;
}

test('BFCache pageshow resumes one polling chain after pagehide', async () => {
  const harness = await dashboardHarness();
  try {
    await harness.load();
    await harness.waitFor(() => harness.requests() === 1 && harness.timers.size === 1, 'initial health poll schedules exactly once');
    harness.window.dispatchEvent(new Event('pagehide'));
    assert.equal(harness.timers.size, 0);
    harness.window.dispatchEvent(restoredPage());
    await harness.waitFor(() => harness.requests() === 2 && harness.timers.size === 1, 'BFCache restore must resume health polling');
    harness.window.dispatchEvent(restoredPage());
    assert.equal(harness.requests(), 2, 'duplicate pageshow must not start another request');
    assert.equal(harness.timers.size, 1, 'exactly one next poll remains scheduled');
    const [id, callback] = harness.timers.entries().next().value;
    harness.timers.delete(id);
    callback();
    await harness.waitFor(() => harness.requests() === 3 && harness.timers.size === 1, 'resumed polling continues');
  } finally { await harness.close(); }
});

test('robot card displays last input and current request from the health snapshot as text', async () => {
  const harness = await dashboardHarness({ robot_id: 'annie-a', room_id: 'demo-room', persona_id: 'annie', state: 'running', last_input: '<b>hello</b>', current_request_id: 'req-123', completed: 0, cancelled: 0, failures: 0, queue_depth: 0 });
  try {
    await harness.load();
    await harness.waitFor(() => harness.timers.size === 1, 'health renders robot card');
    const card = harness.nodes.get('#robots').children[0];
    assert.equal(card.querySelector('.last-input').textContent, '<b>hello</b>');
    assert.equal(card.querySelector('.current-request').textContent, 'req-123');
    const html = await readFile(path.join(root, 'src', 'index.html'), 'utf8');
    assert.match(html, /class="last-input"/);
    assert.match(html, /class="current-request"/);
  } finally { await harness.close(); }
});
