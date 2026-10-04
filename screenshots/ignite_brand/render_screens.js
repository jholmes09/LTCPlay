// Headless screenshots of the branded pages, against mocked engine answers.
// Nothing here talks to a real engine.
const { chromium } = require('/opt/node22/lib/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const REPO = '/home/user/ltcplay/.claude/worktrees/agent-a47d705fd5a5e7dfa';
const WEB = path.join(REPO, 'ltcplay', 'web');
const OUT = process.argv[2] || '/tmp/claude-0/-home-user/aa6b4fe1-21a8-572a-bb1a-806a3305f22e/scratchpad/ignite_screens';
fs.mkdirSync(OUT, { recursive: true });
const BRAND = Object.assign({ email: 'jeff.holmes@hey.com', phone: '404-948-5333', url: '' },
  JSON.parse(fs.readFileSync(path.join(REPO, 'ltcplay_brand.json'), 'utf8')));

const OPS = ['Jeff', 'Andy', 'Sam'];
const SCREENS = ['iPad', 'Rack screen', 'Phone'];
const NAMES = ['front row', 'cat-walk', 'wave flamer', 'pump', 'fireball', 'spare'];
const slots = (cur) => [
  { show: 1, start: '19:00', status: cur > 1 ? 'DONE' : 'NEXT', reason: '' },
  { show: 2, start: '19:45', status: cur === 2 ? 'RUNNING' : cur > 2 ? 'DONE' : 'NEXT', reason: '' },
  { show: 3, start: '20:30', status: cur === 3 ? 'RUNNING' : cur < 3 ? 'PENDING' : 'DONE', reason: '' },
  { show: 4, start: '21:15', status: 'PENDING', reason: '' },
];
function groups(kind) {
  const g = (name, armed, reason = '', dwell = 0, wanted = false) => ({ name, armed, reason, dwell_s: dwell, wanted });
  if (kind === 'off') return NAMES.map((n) => g(n, 'disarmed'));
  if (kind === 'show') return [
    g('front row', 'armed', '', 0, true), g('cat-walk', 'armed', '', 0, true),
    g('wave flamer', 'held', 'cycle the arm'), g('pump', 'held', 're-arm dwell', 3),
    g('fireball', 'disarmed'), g('spare', 'disarmed', 'Show program stopped answering: disarmed. Cycle the arm to re-arm once it is back.')];
  if (kind === 'paused') return [
    g('front row', 'armed', '', 0, true), g('cat-walk', 'held', 'cycle the arm'),
    g('wave flamer', 'held', 're-arm dwell', 2), g('pump', 'armed', '', 0, true),
    g('fireball', 'disarmed'), g('spare', 'disarmed')];
  if (kind === 'aborted') return NAMES.map((n) => g(n, 'held', "Disarmed by the show's Abort. Cycle the arm to re-arm."));
  return NAMES.map((n) => g(n, 'unknown'));
}
function status(sc, me) {
  const base = {
    served_at: Date.now(), fresh_s: 2.0, me,
    disarm_connected: true,
    arming: { enabled: true, signed_in: !!me.signed_in, needs_s: 1.0, fresh_s: 1.0, holds: [] },
    transport: { programming: false, why: 'Programming controls work only when no show is scheduled to run. A show night is loaded.' },
  };
  const fl = (k) => ({ connected: true, stale: false, age_ms: 120, fault: '', groups: groups(k) });
  const sch = (o) => Object.assign({ attached: true, ok: true, error: null, conductor: true, trouble: null,
    current_operator: 'Jeff', operators: OPS, delayed: null, dry_run: false }, o);
  if (sc === 'standby') return Object.assign(base, {
    schedule: sch({ state: 'STANDBY', running: null, held: false, aborted: false,
      next: { show: 3, start: '20:30', in_s: 754 }, slots: slots(2.5) }),
    show: { running: false, show: null, timecode: null, state: null }, flames: fl('off') });
  if (sc === 'show') return Object.assign(base, {
    schedule: sch({ state: 'SHOW', running: 3, held: false, aborted: false,
      next: { show: 4, start: '21:15', in_s: 2236 }, slots: slots(3) }),
    show: { running: true, show: 'Fire and Ice', timecode: '00:07:42:18', state: 'LOCKED' }, flames: fl('show') });
  if (sc === 'paused') return Object.assign(base, {
    schedule: sch({ state: 'PAUSED', running: 3, held: true, aborted: false,
      next: { show: 4, start: '21:15', in_s: 2236 }, slots: slots(3) }),
    show: { running: true, show: 'Fire and Ice', timecode: '00:07:51:02', state: 'PARKED' }, flames: fl('paused') });
  if (sc === 'aborted') return Object.assign(base, {
    schedule: sch({ state: 'STANDBY', running: null, held: false, aborted: true,
      next: { show: 4, start: '21:15', in_s: 2236 }, slots: slots(3.5) }),
    show: { running: false, show: null, timecode: '00:08:03:11', state: null }, flames: fl('aborted') });
  throw new Error(sc);
}

// The engine page's own state (/api/state), idle and running.
function engineState(running) {
  if (!running) return { api: 7, running: false, starting: false, auto_reload: false, stale: [],
    saved_input: { device: 'MOTU UltraLite-mk5', channel: 3 }, last_error: '' };
  return { api: 7, running: true, starting: false, show: 'Fire and Ice, show 3', uptime: 1342,
    state: 'LOCKED', feed_state: 'LOCKED', ltc_in: '00:07:42:18', playing: '00:07:42:18',
    ltc_age: 0.02, source: 'show', sync_ms: 3, blackout_in: null, on_lost: 'freerun', freerun: false,
    rate_in: 30, rate_drop: false, rate_measured: 29.998, rate_confident: true,
    timeline_fps: 30, timeline_drop: false, timeline_rate: '30 non-drop', ltc_frames: 13874, sync_errors: 0,
    level: 0.62, level_verdict: 'good', input: 'MOTU UltraLite-mk5, in 3', input_used: true, input_attached: true,
    no_output: false, universes: 72, frames_out: 55496, send_errors: 0, since_ok: 0.0,
    quiet_dests: 0, rig_total: 22, rig_missing: [], rig_watchable: true,
    now: { name: 'Ignite', file: 'ignite_v14.fseq', seq: '00:02:12.40', seq_total: '00:04:05.00', frame: 3972, seq_frames: 7350,
      tc: '00:05:30:00', ends: '00:09:35:00', elapsed: 132.4, left: 112.6, duration: 245 },
    next: { name: 'Ice Queen', tc: '00:09:40:00', in: 117.6 },
    override: 'auto', has_preshow: true, auto_reload: false, stale: [],
    saved_input: { device: 'MOTU UltraLite-mk5', channel: 3 },
    build: 'ltcplay show-assembly 7c3e1a2', show_build: 'renders 2026-10-03',
    warnings: [], history: ['Controller 10.0.0.114 stopped answering at 19:52:10 and came back 4 s later.'], problems: [], notes: [],
    trigger_available: false };
}

async function mock(page, opts) {
  await page.route('**/*', async (route) => {
    const u = new URL(route.request().url());
    const p = u.pathname;
    const json = (o, st = 200) => route.fulfill({ status: st, contentType: 'application/json', body: JSON.stringify(o) });
    if (p === '/' || p === '/index.html') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(WEB, opts.page)) });
    if (p === '/remote') return route.fulfill({ status: 200, contentType: 'text/html; charset=utf-8', body: fs.readFileSync(path.join(WEB, 'remote.html')) });
    if (p.startsWith('/brand/')) {
      const f = path.join(WEB, 'brand', path.basename(p));
      if (!fs.existsSync(f)) return route.fulfill({ status: 404, body: '' });
      const ct = f.endsWith('.png') ? 'image/png' : f.endsWith('.woff2') ? 'font/woff2' : 'application/octet-stream';
      return route.fulfill({ status: 200, contentType: ct, body: fs.readFileSync(f) });
    }
    if (p === '/api/brand') return json(BRAND);
    if (p === '/api/remote/whoami') return json(opts.me);
    if (p === '/api/remote/status') return json(status(opts.scenario, opts.me));
    if (p === '/api/remote/network') return json({ candidates: ['10.20.0.5'], saved: '10.20.0.5', port: 7878 });
    if (p === '/api/state') return json(engineState(opts.running));
    if (p === '/api/log') return json({ lines: ['19:58:01 show 3 started by the scheduler', '19:58:02 timecode locked, 30 non-drop'] });
    if (p === '/api/timelines') return json({ folder: '/Shows/Fire and Ice', timelines: [{ file: 'fire_ice_2026.json', name: 'Fire and Ice', cues: 9, rate: '30 non-drop' }] });
    if (p === '/api/showdir') return json({ ok: true, folder: '/Shows/Fire and Ice/renders', sequences: 9, options: [{ folder: '/Shows/Fire and Ice/renders', sequences: 9 }] });
    if (p === '/api/devices') return json({ inputs: [{ index: 2, name: 'MOTU UltraLite-mk5', channels: 18, rate: 48000, kind: 'hardware' }], candidates: [2], saved: { device: 'MOTU UltraLite-mk5', channel: 3 } });
    return json({ error: 'no such thing here' }, 404);
  });
}

const VIEWS = {
  rack: { viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 },
  rack4k: { viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 2 },
  ipadP: { viewport: { width: 820, height: 1180 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
  ipadL: { viewport: { width: 1180, height: 820 }, deviceScaleFactor: 2, isMobile: true, hasTouch: true },
};
const LOCAL = { local: true, signed_in: true, who: 'Jeff', device: 'Rack screen', fresh_s: 2.0, operators: OPS, screens: SCREENS };
const IPAD_OUT = { local: false, signed_in: false, fresh_s: 2.0, operators: OPS, screens: SCREENS };
const IPAD_IN = { local: false, signed_in: true, who: 'Jeff', device: 'iPad', fresh_s: 2.0, operators: OPS, screens: SCREENS };

async function shot(browser, name, view, opts, after) {
  const ctx = await browser.newContext(VIEWS[view]);
  const page = await ctx.newPage();
  await mock(page, opts);
  await page.goto('http://showpc.local:7878' + (opts.url || '/'));
  await page.evaluate(() => document.fonts.ready);
  await page.waitForTimeout(1200);
  if (after) await after(page);
  await page.screenshot({ path: path.join(OUT, name + '.png'), fullPage: !!opts.full });
  console.log('wrote', name);
  await ctx.close();
}

(async () => {
  const browser = await chromium.launch({ executablePath: '/opt/pw-browsers/chromium-1194/chrome-linux/chrome' });
  // The rack monitor (the remote page on the show machine itself).
  for (const sc of ['standby', 'show', 'paused', 'aborted']) {
    await shot(browser, `rack_1920_${sc}`, 'rack', { page: 'remote.html', url: '/remote', me: LOCAL, scenario: sc });
  }
  await shot(browser, 'rack_4k_200pct_show', 'rack4k', { page: 'remote.html', url: '/remote', me: LOCAL, scenario: 'show' });
  await shot(browser, 'rack_1920_show_fullpage', 'rack', { page: 'remote.html', url: '/remote', me: LOCAL, scenario: 'show', full: true });
  // The engine's operator page.
  await shot(browser, 'operator_1920_idle', 'rack', { page: 'index.html', running: false, full: true });
  await shot(browser, 'operator_1920_running', 'rack', { page: 'index.html', running: true, full: true });
  await shot(browser, 'operator_ipad_portrait_running', 'ipadP', { page: 'index.html', running: true, full: true });
  // The iPad remote.
  for (const v of ['ipadP', 'ipadL']) {
    const o = v === 'ipadP' ? 'portrait' : 'landscape';
    await shot(browser, `ipad_${o}_signin`, v, { page: 'remote.html', me: IPAD_OUT, scenario: 'standby' });
    await shot(browser, `ipad_${o}_controls_show`, v, { page: 'remote.html', me: IPAD_IN, scenario: 'show' });
    await shot(browser, `ipad_${o}_controls_paused`, v, { page: 'remote.html', me: IPAD_IN, scenario: 'paused' });
    await shot(browser, `ipad_${o}_arming`, v, { page: 'remote.html', me: IPAD_IN, scenario: 'standby' }, async (page) => {
      await page.evaluate(() => {
        document.getElementById('fl-lamps').scrollIntoView({ block: 'start' });
        window.scrollBy(0, -70);
        const f = document.querySelector('[data-arm="1"] .fill');
        if (f) f.style.width = '62%';
        document.getElementById('arm-note').textContent = 'Keep holding...';
      });
    });
    await shot(browser, `ipad_${o}_full_show`, v, { page: 'remote.html', me: IPAD_IN, scenario: 'show', full: true });
  }
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
