// A small in-memory engine for the phone page (ltcplay/web/phone.html).
// Serves the page at /phone, the brand files, and every route the page calls,
// from a rig you can reshape. Never talks to a rig. Used by phone_test.js.
const http = require("http");
const fs = require("fs");
const path = require("path");
const REPO = process.env.LTC_REPO || path.join(__dirname, "..", "..");

function makeRig(o) {
  o = Object.assign({groups: 4, machines: 3, hazers: 2, lasers: 3, long: false, users: false}, o || {});
  const pad = (s) => (o.long ? s + " the very long name of forty chars".slice(0, 40 - s.length - 1) : s);
  const groups = [], machines = [];
  for (let g = 0; g < o.groups; g++) {
    const name = o.long ? ("G" + g + " " + "Wave Flamer Scaffold Left Row Of Machines Here").slice(0, 40) : "Group " + (g + 1);
    const ids = [];
    for (let m = 0; m < (Array.isArray(o.machines) ? o.machines[g] : o.machines); m++) {
      const id = "m" + g + "_" + m;
      machines.push({id, name: o.long ? (name.slice(0, 33) + " unit " + (m + 1)).slice(0, 40) : name + " unit " + (m + 1), kind: "flame", group: name});
      ids.push(id);
    }
    groups.push({name, ids, armed: "disarmed"});
  }
  const hazers = [];
  for (let i = 0; i < o.hazers; i++) hazers.push({name: o.long ? ("Hazer " + i + " long name of forty chars exactly x").slice(0, 40) : "Hazer " + (i + 1), armed: false, haze: 50, fan: 40});
  const lasers = [];
  for (let i = 0; i < o.lasers; i++) lasers.push({id: "L" + i, name: o.long ? ("Laser " + i + " long name of forty chars exactly x").slice(0, 40) : "Laser " + (i + 1), armed: false, locked: false});
  const rig = {
    o, groups, machines, hazers, lasers, locked: new Set(), laserTests: new Set(), hzTest: {}, runs: [], runSeq: 0,
    alerts: [], flamesConnected: true, flamesStale: false, hazersDown: "",
    users: o.users, user: o.users ? null : "Jeff", role: "admin", may: {show: true, arm: true, unlock: true},
    showControl: true, state: "STANDBY", length: 460, showStartedAt: 0,
    preroll: {state: "idle", window_s: 10, items: []}, prerollAt: 0, showCheck: null,
    statusDead: false, statusDelayMs: 0, refuse: {}, armDelay: 700, armOutcome: "armed", ignore: new Set(),
    posts: [], tickets: {}, nextIn: 754, stopHeld: false, held: false,
  };
  return rig;
}

function statusOf(rig) {
  const now = Date.now();
  const gs = rig.groups.map((g) => {
    const c = rig.machines.filter((m) => m.group === g.name);
    return {name: g.name, armed: rig.flamesStale ? "unknown" : g.armed, wanted: g.armed !== "disarmed", reason: "", dwell_s: 0};
  });
  const gm = {};
  rig.groups.forEach((g) => { gm[g.name] = {machines: g.ids.length, locked: g.ids.filter((id) => rig.locked.has(id)).length, lock: "ok"}; });
  const lockedNames = rig.machines.filter((m) => rig.locked.has(m.id)).map((m) => m.name);
  const live = rig.state === "SHOW" || rig.state === "PAUSED";
  const left = live ? Math.max(0, rig.length - (now - rig.showStartedAt) / 1000) : 0;
  const st = {
    served_at: now, fresh_s: 2.0,
    me: {local: false, device: "iPad at 10.0.0.9", screen: "iPad", fresh_s: 2.0, user: rig.user, role: rig.user ? rig.role : null,
      users_active: rig.users, may: rig.user || !rig.users ? Object.assign({settings: false, users: false, checklist: true, show_files: true}, rig.may) : {show: false, arm: false, unlock: false},
      view_only: rig.users && !rig.user},
    schedule: {attached: true, ok: true, state: rig.state, running: live ? 1 : null, held: rig.state === "HOLD" || rig.state === "PAUSED", stop_held: rig.stopHeld,
      next: live ? null : {show: 3, start: "19:30", in_s: rig.nextIn, in_s_exact: rig.nextIn},
      start_blocked: null,
      slots: [["19:00", "DONE"], ["19:15", "DONE"], ["19:30", live ? "RUNNING" : "NEXT"], ["19:45", "PENDING"], ["20:00", "PENDING"]].map(([s, t], i) => ({show: i + 1, start: s, status: t}))},
    show: {running: live, starting: false, show_control_on: rig.showControl, show: live || rig.state === "HOLD" ? "Ignite the Night" : null, timeline: null},
    countdown: live ? {kind: "left", label: "Left", s: left, length: rig.length, running: rig.state === "SHOW"} : {kind: "next", label: "Next", s: rig.nextIn, running: true},
    transport: {}, subsystems: {},
    flames: rig.flamesConnected ? {connected: true, stale: rig.flamesStale, age_ms: 100, groups: gs} : {connected: false, stale: true, groups: [], why: "x"},
    arming: {enabled: true, needs_s: 1, fresh_s: 2.5, holds: [], confirm: {runs: rig.runs.map((r) => ({group: 0, name: r.name, run: r.run, state: r.state, text: r.text, who: "Jeff", mine: true})), waiting: [], ticket_s: 5, min_s: 0.3, max_s: 3}},
    morning: {locked_machines: lockedNames, locked_lasers: [], flamesafe_confirmed: true, lock_error: "", group_machines: gm,
      laser_tests_on: [...rig.laserTests], laser_arm: {armed: [], total: rig.lasers.length}, purge: false, open: false},
    laser_arm: rig.lasers.length ? {lasers: rig.lasers.map((l) => ({id: l.id, name: l.name, armed: l.armed, locked: l.locked})), groups: {"All lasers": rig.lasers.map((l) => l.id)}, armed: 0, total: rig.lasers.length} : null,
    alerts: rig.alerts, alert_hold: {held: false}, preroll: Object.assign({}, rig.preroll, {age_s: rig.prerollAt ? (now - rig.prerollAt) / 1000 : null}),
  };
  if (rig.hazers.length) {
    st.hazers = {available: true, down: rig.hazersDown, hazers: rig.hazers.map((h) => ({name: h.name, armed: h.armed, state: h.armed ? "standby" : "disarmed", haze_pct: h.haze, fan_pct: h.fan}))};
    const on = Object.keys(rig.hzTest);
    st.hazer_test = {available: true, hazers: rig.hazers.map((h) => ({name: h.name, on: !!rig.hzTest[h.name], state: rig.hzTest[h.name] ? "on" : "off", left_s: rig.hzTest[h.name] ? Math.max(0, Math.round((rig.hzTest[h.name] - now) / 1000)) : 0})), on: on.length > 0, beat_s: 2, max_s: 60};
  }
  if (rig.laserBad) st.laser_arm = {error: "x", lasers: []};
  return st;
}

function morningOf(rig) {
  const groups = {};
  rig.groups.forEach((g) => { groups[g.name] = g.ids; });
  return {machines: rig.machines, groups, lockout: {machines: {}, lasers: {}}, lasers: [], me: {}};
}

function start(rig, port) {
  const server = http.createServer((req, res) => {
    const u = new URL(req.url, "http://x");
    const p = u.pathname;
    const send = (code, body, type) => { res.writeHead(code, {"Content-Type": type || "application/json", "Cache-Control": "no-store"}); res.end(typeof body === "string" || Buffer.isBuffer(body) ? body : JSON.stringify(body)); };
    if (p === "/phone") return send(200, fs.readFileSync(path.join(REPO, "ltcplay", "web", "phone.html")), "text/html; charset=utf-8");
    if (p.startsWith("/brand/")) {
      const f = path.join(REPO, "ltcplay", "web", "brand", path.basename(p));
      if (fs.existsSync(f)) return send(200, fs.readFileSync(f), f.endsWith(".png") ? "image/png" : "font/woff2");
      return send(404, {});
    }
    if (p === "/favicon.ico" || p.startsWith("/icon") || p.startsWith("/apple")) return send(404, "");
    if (req.method === "GET") {
      if (p === "/api/remote/status") {
        if (rig.statusDead) return req.socket.destroy();
        const go = () => send(200, statusOf(rig));
        return rig.statusDelayMs ? setTimeout(go, rig.statusDelayMs) : go();
      }
      if (p === "/api/remote/morning") return send(200, morningOf(rig));
      if (p === "/api/remote/users") return send(200, {active: rig.users, names: ["Jeff", "Sam"], me: {}, local: false});
      return send(200, {});
    }
    let buf = "";
    req.on("data", (c) => (buf += c));
    req.on("end", () => {
      let b = {}; try { b = JSON.parse(buf || "{}"); } catch (e) {}
      const name = p.replace("/api/remote/", "");
      rig.posts.push({name, body: b, at: Date.now()});
      if (rig.ignore.has(name)) return;           // never answers
      if (rig.refuse[name]) return send(409, {error: rig.refuse[name]});
      const ok = (x) => send(200, Object.assign({ok: true, text: "ok"}, x || {}));
      const grp = (n) => rig.groups.find((g) => g.name === n);
      switch (name) {
        case "arm-request": { if (!grp(b.group)) return send(400, {error: "That is not a flame group flamesafe reports."});
          const t = "t" + Math.random().toString(36).slice(2); rig.tickets[b.group] = {t, at: Date.now()};
          return ok({ticket: t, expires_s: 5, min_s: 0.3, group: b.group}); }
        case "arm-confirm": { const k = rig.tickets[b.group]; const age = k ? Date.now() - k.at : 0;
          if (!k || k.t !== b.ticket) return send(409, {error: "That Arm was not waiting for a Confirm from this screen. Tap Arm again. Nothing was armed."});
          if (age < 300) return send(409, {error: "Confirm came with the Arm tap itself. Tap Arm, then Confirm. Nothing was armed."});
          delete rig.tickets[b.group];
          const run = {run: ++rig.runSeq, name: b.group, state: "holding", text: ""}; rig.runs.push(run);
          setTimeout(() => {
            if (rig.armOutcome === "armed") { grp(b.group).armed = "armed"; run.state = "armed"; }
            else if (rig.armOutcome === "timeout") { run.state = "timeout"; run.text = "The Stream Deck program did not take the arm within 3 s. Nothing was armed by this press. Check the group's lamp."; }
            else { run.state = "refused"; run.text = "The group is locked out. Nothing was armed."; }
          }, rig.armDelay);
          return ok({run: run.run, state: "holding", group: b.group}); }
        case "group-disarm": grp(b.group).armed = "disarmed"; return ok({confirmed: true});
        case "disarm-all": rig.groups.forEach((g) => (g.armed = "disarmed")); return ok({plain: true});
        case "hazers-off": rig.hazers.forEach((h) => (h.armed = false)); rig.hzTest = {}; return ok();
        case "laser-arm": if (b.on === true) { b.lasers.forEach((id) => (rig.lasers.find((l) => l.id === id).armed = true)); } else { rig.lasers.forEach((l) => { if (b.all || (b.lasers || []).includes(l.id)) { l.armed = false; rig.laserTests.delete(l.id); } }); } return ok();
        case "lock": { const m = rig.machines.find((x) => x.id === b.id); if (!m) return send(409, {error: "There is no machine called " + b.id});
          if (b.on) rig.locked.add(b.id); else { if (b.confirmed !== true) return send(409, {error: "Unlocking needs a confirm."}); rig.locked.delete(b.id); } return ok(); }
        case "hazer": { const h = rig.hazers.find((x) => x.name === b.hazer); h.armed = b.on === true; return ok(); }
        case "hazers-arm-all": rig.hazers.forEach((h) => (h.armed = true)); return ok({armed: rig.hazers.map((h) => h.name), not_armed: []});
        case "hazer-level": rig.hazers.forEach((h) => { if (b.hazer === "all" || b.hazer === h.name) { if (b.haze != null) h.haze = b.haze; if (b.fan != null) h.fan = b.fan; } }); return ok();
        case "hazer-test": if (b.on === true) { (b.hazers === "all" ? rig.hazers.map((h) => h.name) : b.hazers).forEach((n) => (rig.hzTest[n] = Date.now() + 60000)); return ok(); }
          if (b.beat) return ok(); if (b.hazers) b.hazers.forEach((n) => delete rig.hzTest[n]); else rig.hzTest = {}; return ok();
        case "laser-test": { const set = b.all ? rig.lasers : rig.lasers.filter((l) => l.id === b.laser);
          set.forEach((l) => { if (b.on) { if (l.armed) rig.laserTests.add(l.id); } else rig.laserTests.delete(l.id); }); return ok(); }
        case "start-now": rig.state = "SHOW"; rig.showStartedAt = Date.now(); return ok();
        case "resume": rig.state = "SHOW"; rig.showStartedAt = Date.now() - 100000; return ok();
        case "hold": rig.state = "PAUSED"; return ok();
        case "stop": rig.state = "HOLD"; rig.stopHeld = true; rig.groups.forEach((g) => (g.armed = "disarmed")); return ok({stopped: true});
        case "alert": rig.alerts = rig.alerts.filter((a) => a.id !== b.id); return ok();
        case "preroll-continue": if (b.phase === "done") { rig.preroll = {state: "passed", items: [], continued: {by: "Jeff", off: ["VIDEO"]}}; rig.showCheck = null; rig.state = "SHOW"; rig.showStartedAt = Date.now(); } return ok({holding: true, hold_s: 1.0});
        case "signin": if (b.password === "goodpass") { rig.user = b.name; return ok(); } return send(401, {error: "That name and password do not match."});
        case "signout": rig.user = null; return ok();
        default: return ok();
      }
    });
  });
  return new Promise((r) => server.listen(port || 0, "127.0.0.1", () => r({server, port: server.address().port})));
}
module.exports = {makeRig, start, statusOf};
