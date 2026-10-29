// A fake LTC Player engine for the UI audit: answers every /api/remote/*
// route from a mutable state object, so every operator state can be drawn.
// Built from the real engine's own status (fx/real_status.json) and the
// selftest's Pico fixtures (fx/*.json). Never talks to a rig.
const fs = require("fs");
const path = require("path");
const ROOT = __dirname;
const REPO = process.env.LTC_REPO || path.join(ROOT, "..", "..");
const FX = p => JSON.parse(fs.readFileSync(path.join(ROOT, "fx", p + ".json"), "utf8"));
const clone = o => JSON.parse(JSON.stringify(o));

const PERMS = ["show", "arm", "unlock", "checklist", "show_files", "settings", "users"];
const ROLE_MAY = {
  admin: Object.fromEntries(PERMS.map(p => [p, true])),
  operator: {show: true, arm: true, unlock: true, checklist: true, show_files: false, settings: false, users: false},
  viewer: Object.fromEntries(PERMS.map(p => [p, false])),
  none: Object.fromEntries(PERMS.map(p => [p, false])),
};
function me(user, role){
  return {local: true, device: "Rack screen", screen: "Rack screen", fresh_s: 2.0, user: user || null,
    role: role || null, users_active: true, may: ROLE_MAY[role || "none"], view_only: !user,
    machine_user: null, audit_fault: ""};
}

function baseStatus(){
  const real = FX("real_status");
  const pico = FX("status_pico");
  const s = clone(real);
  s.me = me("Dallen", "operator");
  s.version = "gpl-2026.10.28.1, build 7d43fbbe95";
  s.schedule.slots = s.schedule.slots.slice(0, 12);
  s.schedule.dry_run = false;
  s.schedule.next = {show: 4, start: "19:30", in_s: 900, in_s_exact: 900};
  s.countdown = {kind: "next", label: "Next show", s: 900, running: true};
  s.subsystems = [
    {name: "Deck", state: "ok", word: "ready"}, {name: "Flamesafe", state: "ok", word: "live"},
    {name: "Audio", state: "ok", word: "ready"}, {name: "Laser arm", state: "ok", word: "disarmed"},
    {name: "Video", state: "ok", word: "linked"}, {name: "Timecode", state: "ok", word: "ready"}];
  s.flames = {connected: true, stale: false, age_ms: 120, fault: "", cues_fault: "",
    groups: [{name: "front row", armed: "disarmed"}, {name: "cat-walk", armed: "disarmed"},
             {name: "wave flamer", armed: "disarmed"}, {name: "fireball", armed: "disarmed"}],
    aim: {ltcplay: null, flamesafe: null, path: "", agree: true}};
  s.deck = {heard: true, age_s: 0.4, text: ""};
  s.disarm_connected = true;
  s.arming = {enabled: true, needs_s: 2.0, fresh_s: 1.0, holds: []};
  s.laser_fault = "";
  s.alerts = [];
  s.artnet_note = "";
  s.booth = {configured: true, device: "Booth speaker"};
  s.announce = {configured: true, ok: true, error: null, device: "MOTU M4", device_set: true,
    device_available: true, device_reason: null, blocked: null, over_show: false,
    route: {kind: "device", ok: true, text: "MOTU M4"},
    items: [{id: "delayed", name: "Delayed", available: true, missing: false, reason: null, length_s: 12.0},
            {id: "cancellation", name: "Cancellation", available: true, missing: false, reason: null, length_s: 9.0},
            {id: "cannot_continue", name: "Cannot continue", available: true, missing: false, reason: null, length_s: 14.0}],
    playing: null};
  s.morning = {locked_machines: [], locked_lasers: [], flamesafe_confirmed: true, lock_error: "",
    flamesafe_extra: [], purge: false, laser_tests_on: [], open: false, config_error: "", purge_enabled: true,
    laser_arm: {armed: [], total: 4, output_permitted: false, output_fault: ""},
    group_machines: {"front row": {machines: 3, locked: 0}, "cat-walk": {machines: 3, locked: 0},
                     "wave flamer": {machines: 3, locked: 0}, "fireball": {machines: 1, locked: 0}}};
  s.fuel = {refill_soon: 0, fault: false};
  s.fireball = {available: true, on: [], owner: null};
  s.show_folder = {served: "C:\\Users\\show\\Documents\\LTC Shows", problem: ""};
  s.setup = Object.assign({}, s.setup, {show_folder: "C:\\Users\\show\\Documents\\LTC Shows",
    schedule_path: "C:\\Users\\show\\AppData\\Local\\ltcplay\\ltcplay_schedule.json"});
  s.transport = {programming: false, why: "Nothing is playing. To scrub, on the show machine choose Programming on the Show file card and press Run.", freerun: false, paused: false, loop: null, marks: {}};
  s.live_flame = {available: true, on: false, why: ""};
  s.programming_flames = {available: true, follows: true, on: false, line: "Flames follow the show while you play here. Only armed groups fire. Hold, Stop or Abort ends it."};
  // The hazers (hazers.py, 2026-10-29): Jeff's three, all disarmed.
  s.hazers = {available: true, down: "", last_off: null, agree: true, show: "idle", levels_fault: "",
    hazers: [1, 2, 3].map(n => ({name: "Hazer " + n, haze_slot: 78 + 2 * n, fan_slot: 79 + 2 * n, armed: false,
      label: "Hazer " + n + " DISARMED", state: "disarmed", haze: 102, fan: 153, haze_pct: 40, fan_pct: 60, why: null}))};
  return s;
}

const SCENARIOS = {
  idle_operator: s => s,
  signed_out: s => { s.me = me(null, null); },
  admin: s => { s.me = me("Jeff", "admin"); },
  show_pending: s => { s.show = Object.assign({}, s.show, {running: true});  s.schedule.next = {show: 4, start: "19:30", in_s: 45, in_s_exact: 45}; s.countdown = {kind: "next", label: "Next show", s: 45, running: true};
    s.flames.groups.forEach(g => g.armed = "armed"); s.subsystems[3].word = "armed"; },
  running: s => { Object.assign(s.schedule, {state: "SHOW", running: 4, next: {show: 5, start: "19:50", in_s: 1500, in_s_exact: 1500}});
    s.show = {running: true, starting: false, show: "Fire and Ice", timeline: "fireice_timeline.json", timecode: "01:03:12:08", state: "playing", rate_note: ""};
    s.countdown = {kind: "left", label: "Time left", s: 312, running: true};
    s.flames.groups.forEach(g => g.armed = "armed");
    s.laser_arm.lasers.forEach(l => l.armed = true); s.subsystems[3].word = "armed"; },
  held: s => { SCENARIOS.running(s); Object.assign(s.schedule, {state: "PAUSED", held: true}); s.countdown.running = false; },
  aborted: s => { SCENARIOS.running(s); Object.assign(s.schedule, {state: "STANDBY", running: null, aborted: true});
    s.show.running = false; s.flames.groups.forEach(g => { g.armed = "held"; g.reason = "Disarmed by the show's Abort. Cycle the arm to re-arm."; });
    s.laser_arm.lasers.forEach(l => l.armed = false); s.countdown = {kind: "next", label: "Next show", s: 1500, running: true}; },
  alerts_open: s => { const p = FX("status_pico"); s.alerts = p.alerts; s.flames.cues_fault = p.flames.cues_fault;
    s.deck = p.deck; s.morning.locked_machines = ["wave flamer 3"]; s.disk = p.disk;
    s.subsystems[0] = {name: "Deck", state: "bad", word: "missing"}; s.subsystems[5] = {name: "Timecode", state: "bad", word: "fault"};
    s.flames.groups[2].aim = p.flames.groups[2].aim; },
  armed_groups: s => { s.flames.groups[0].armed = "armed"; s.flames.groups[1].armed = "armed";
    s.flames.groups[2].armed = "held"; s.flames.groups[2].dwell_s = 3; s.flames.groups[2].reason = "re-arm dwell";
    s.laser_arm.lasers[0].armed = true; s.laser_arm.lasers[1].armed = true; },
  locked: s => { s.morning.locked_machines = ["wave flamer 3", "cat-walk 2"]; s.morning.locked_lasers = ["Laser 3"];
    s.morning.group_machines["wave flamer"].locked = 1; s.morning.group_machines["cat-walk"].locked = 1;
    s.laser_arm.lasers[2].locked = true; },
  programming: s => { s.me = me("Jeff", "admin"); s.transport = {programming: true, why: "", freerun: false, paused: false, loop: ["01:00:30:00", "01:01:10:00"], marks: {a: "01:00:30:00", b: "01:01:10:00"}, playhead: "show_audio", playing: true};
    s.show = {running: true, starting: false, show: "Fire and Ice", timeline: "fireice_timeline.json", timecode: "01:00:44:12", state: "playing", rate_note: ""}; },
  announcing: s => { s.show = Object.assign({}, s.show, {running: true});  s.schedule.held = true; s.schedule.state = "STANDBY";
    s.announce.playing = {id: "delayed", name: "Delayed", elapsed_s: 4, length_s: 12, held_schedule: true}; },
  alert_hold: s => { s.show = Object.assign({}, s.show, {running: true});  s.alert_hold = {held: true, text: "Held at showtime: an alert is open. Acknowledge it, then press Resume (or Start now)."};
    const p = FX("status_pico"); s.alerts = [p.alerts[0]]; s.schedule.next = {show: 4, start: "19:30", in_s: 0, in_s_exact: 0}; },
  live_flame: s => { const p = FX("status_live_flame"); s.me = me("Jeff", "admin"); s.live_flame = p.live_flame; s.artnet_note = p.artnet_note; s.flames.groups[0].armed = "armed"; },
  getting_started: s => { const p = FX("status_nofile"); Object.assign(s, {setup: p.setup || s.setup}); s.selected_show = null; s.schedule = Object.assign(s.schedule, p.schedule || {}); },
  ready: s => { s.show = Object.assign({}, s.show, {running: true}); },
  weather_alert: s => { s.flames.groups[0].armed = "armed"; },
  stale: s => s,
  rig_checking: s => { s.show = Object.assign({}, s.show, {running: true}); s.schedule.next = {show: 4, start: "19:30", in_s: 50, in_s_exact: 50};
    s.countdown = {kind: "next", label: "Next show", s: 50, running: true}; s.preroll = {state: "checking", label: "show 4", items: [], text: ""}; },
  rig_failed: s => { s.show = Object.assign({}, s.show, {running: true}); s.schedule.next = {show: 4, start: "19:30", in_s: 0, in_s_exact: 0};
    s.countdown = {kind: "next", label: "Next show", s: 0, running: true};
    s.preroll = {state: "failed", label: "show 4", age_s: 20, manual: false, continue_hold_s: 1.0, items: [], text: "",
      show_check: {label: "show 4", off: ["video"], items: [{name: "Video", ok: false, required: true, text: "MadMapper did not move"}]}};
    s.alerts = [{id: 77, kind: "preroll", severity: "fault", text: "Rig check FAILED before show 4: Video: MadMapper did not move.", options: ["Acknowledge"]}];
    s.alert_hold = {held: true, text: "Held at showtime: an alert is open."}; },
  rig_continued: s => { SCENARIOS.running(s); s.preroll = {state: "failed", label: "show 4", continued: {by: "Dallen", off: ["video"], failed: "Video"}}; },
  all_live: s => { s.me = me("Jeff", "admin"); s.cleaning = {available: true, state: "phase2", text: "Phase 2 of 2: purge bursts"};
    s.live_flame = {available: true, on: true, who: "Jeff"}; },
  hazers_on: s => { SCENARIOS.running(s); s.hazers.show = "playing"; s.hazers.hazers.forEach((h, i) => { h.armed = i !== 1; h.state = h.armed ? "running" : "disarmed"; }); },
  hazers_held: s => { SCENARIOS.held(s); s.hazers.show = "held"; s.hazers.hazers.forEach(h => { h.armed = true; h.state = "held"; }); },
  flames_unknown: s => { s.flames.connected = false; s.flames.why = "flamesafe is not connected."; s.show = Object.assign({}, s.show, {running: true}); },
  rehearsal: s => { SCENARIOS.programming(s); },
  cleaning: s => { s.cleaning = {available: true, state: "phase1", text: "Phase 1 of 2: firing the wave flamers"}; },
  // A timed purge running (morning.py, 2026-10-29): the engine's countdown.
  timed_purge: s => { s.morning.timed = {left_s: 17.4, seconds: 30, elapsed_s: 12.6, machines: ["front row 1", "front row 2", "front row 3", "cat-walk 1"],
    device: "iPad at 10.20.0.44", who: "Dallen"}; s.flames.groups[0].armed = "armed"; s.flames.groups[1].armed = "armed"; },
  // The Stream Deck program reads the engine but does not act on a group's
  // Disarm: the engine forces the flame link's disarm_all (2026-10-29).
  deck_not_answering: s => { s.flames.groups[0].armed = "armed"; s.flames.groups[0].wanted = true; },
};

// What the other GET routes answer, per scenario.
function routesFor(name, st){
  const r = {};
  r["/api/remote/whoami"] = Object.assign({}, st.me, {fresh_s: 2.0});
  r["/api/remote/setup"] = FX("real_setup");
  const mo = name === "locked" ? (() => { const m = FX("morning"); m.lockout = {machines: {m8: {by: "Andy", at: 0}}, lasers: {}}; return m; })()
           : name === "purge" ? FX("morning_firing") : FX("morning");
  // The timed purge's numbers, as the engine sends them (2026-10-29), and
  // one running in the timed_purge scenario.
  mo.timed_settings = {presets: [2, 5, 10, 20, 30], default_s: 5, max_s: 30, min_s: 1, limit_s: 120, hold_s: 1.0, error: ""};
  const tm = st.morning && st.morning.timed;
  mo.timed = Object.assign({running: !!tm, left_s: null, seconds: null, elapsed_s: null, machines: [], firing: [], waiting: [], device: null,
    who: "", holding: false, holder: null, mine: false, held_s: null, hold_s: 1.0, last: ""}, tm ? Object.assign({running: true}, tm) : {});
  if(tm){ mo.purge_state = Object.assign({}, mo.purge_state, {timed: true, firing: true, selected: ["m1", "m2", "m3", "m4"]}); }
  r["/api/remote/morning"] = mo;
  const fu = FX("fuel");
  if(!st.me.user){ fu.may_refill = false; fu.may_undo = false; fu.refill_why = "Sign in as an operator or admin to log a refill (the user chip at the top)."; }
  else if(st.me.role !== "admin") fu.may_undo = false;
  r["/api/remote/fuel"] = fu;
  r["/api/remote/fireball"] = FX("fireball");
  const wx = FX("weather");
  r["/api/remote/weather"] = wx;
  const users = FX("real_users");
  users.me = {name: st.me.user, role: st.me.role};
  users.users.forEach(u => u.password_set = true);
  r["/api/remote/users"] = users;
  r["/api/remote/schedule-file"] = FX("real_schedule-file");
  r["/api/timelines"] = {folder: "C:\\Users\\show\\Documents\\LTC Shows", timelines: [{file: "fireice_timeline.json"}, {file: "fireice_rehearsal.json"}]};
  r["/api/remote/network"] = {address: "10.20.0.5", choices: ["10.20.0.5", "192.168.1.20"]};
  r["/api/schedule/operator"] = {current: "Dallen", operators: ["Andy", "Dallen", "Dave", "Jared", "Jeff"]};
  r["/api/brand"] = {};
  return r;
}

class FakeEngine {
  constructor(name){
    this.name = name;
    this.st = baseStatus();
    (SCENARIOS[name] || (s => s))(this.st);
    this.posts = [];
    this.dead = false;
    this.html = fs.readFileSync(path.join(REPO, "ltcplay", "web", "remote.html"), "utf8");
    this.html2 = fs.readFileSync(path.join(REPO, "ltcplay", "web", "remote2.html"), "utf8");
    const a = this.html.indexOf('<script id="remote-logic">'), b = this.html.indexOf("</script>", a);
    this.logic = this.html.slice(a + '<script id="remote-logic">'.length, b);
    this.uiDefault = 1;
  }
  setScenario(name){ this.name = name; this.st = baseStatus(); (SCENARIOS[name] || (s => s))(this.st); this.holds = {}; }
  // The real engine's hold registry (remote.py arm_hold): one hold per
  // group with its own id; a beat with any other id is refused, and any
  // disarm, Abort or Stop ends it.
  dropHolds(group){ this.holds = this.holds || {}; if(group === undefined) this.holds = {}; else delete this.holds[group]; }
  groupDisarm(i){
    const s = this.st, g = s.flames.groups[i];
    if(!g) return {code: 400, body: {error: "That is not a flame group flamesafe reports."}};
    this.dropHolds(i);
    if(this.name === "deck_not_answering"){
      const text = "Not confirmed: ALL groups disarm sent. Disarm " + g.name + " forced (flamesafe did not report " + g.name +
        " off within 1 s of the Disarm): the flame link has no one-group disarm, so the Abort's disarm was sent to every flame group, " +
        "every hazer is off, and the show's flames stay at zero until Hold then Resume or a new run. Each group needs Arm again " +
        "(let go, then hold) before it can fire.";
      s.flames.groups.forEach(x => { x.armed = "disarmed"; x.wanted = false; x.reason = "Disarmed by the show's Abort. Cycle the arm to re-arm."; });
      s.alerts = (s.alerts || []).concat([{id: 900 + i, kind: "flames", severity: "fault", text: text, options: ["Acknowledge"]}]);
      return {code: 200, body: {ok: true, confirmed: false, forced: true, text: text}};
    }
    g.armed = "disarmed"; g.wanted = false;
    return {code: 200, body: {ok: true, confirmed: true, text: "Disarm " + g.name + " confirmed: flamesafe reports it off."}};
  }
  onPost(route, body){
    const s = this.st, sch = s.schedule;
    this.posts.push(route);
    const ok = text => ({code: 200, body: {ok: true, text: text || "Done."}});
    switch(route){
      case "/api/remote/signin": {
        const role = (FX("real_users").users.find(u => u.name === body.name) || {}).role || "operator";
        s.me = me(body.name, role); return ok("Signed in as " + body.name + ".");
      }
      case "/api/remote/signout": s.me = me(null, null); return ok("Signed out.");
      case "/api/remote/hold": sch.held = true; if(sch.state === "SHOW") sch.state = "PAUSED"; return ok("Held.");
      case "/api/remote/resume": sch.held = false; if(sch.state === "PAUSED") sch.state = "SHOW"; s.alert_hold = {held: false};
        if(s.announce) s.announce.playing = null; return ok("Resumed.");
      case "/api/remote/abort": this.dropHolds(); sch.aborted = true; sch.state = "STANDBY"; sch.running = null; if(s.show) s.show.running = false;
        s.flames.groups.forEach(g => { g.armed = "held"; g.reason = "Disarmed by the show's Abort. Cycle the arm to re-arm."; });
        s.laser_arm.lasers.forEach(l => l.armed = false); return ok("Aborted.");
      case "/api/remote/reset": sch.aborted = false; s.flames.groups.forEach(g => { g.armed = "disarmed"; g.wanted = false; delete g.reason; }); return ok("Reset.");
      case "/api/remote/start-now": sch.state = "SHOW"; sch.running = (sch.next || {}).show || 1; sch.held = false;
        s.show = {running: true, show: "Fire and Ice", timecode: "01:00:01:00", state: "playing"}; s.countdown = {kind: "left", label: "Time left", s: 440, running: true}; return ok("Started.");
      case "/api/start": s.show = Object.assign({}, s.show || {}, {running: true, starting: false, show: "Fire and Ice", timeline: body.timeline, state: "idle"});
        if(sch.start_blocked) sch.start_blocked = null; return {code: 200, body: {ok: true, banner: "Running " + body.timeline + "."}};
      case "/api/remote/ui-default": this.uiDefault = body.default; return ok("Set.");
      case "/api/remote/stop": this.dropHolds(); sch.state = "STANDBY"; sch.running = null; s.show = {running: false};
        s.flames.groups.forEach(g => g.armed = "disarmed"); return ok("Stopped.");
      case "/api/remote/disarm-all": this.dropHolds(); s.flames.groups.forEach(g => { g.armed = "disarmed"; g.wanted = false; }); return ok("Disarmed.");
      case "/api/remote/hazer": { const h = s.hazers.hazers.find(x => x.name === body.hazer);
        if(h){ h.armed = body.on === true; h.state = h.armed ? "standby" : "disarmed"; } return ok((body.hazer || "Hazer") + (body.on === true ? " is ARMED." : " is DISARMED.")); }
      case "/api/remote/hazers-off": s.hazers.hazers.forEach(h => { h.armed = false; h.state = "disarmed"; }); return ok("Every hazer is DISARMED.");
      case "/api/remote/hazer-level": s.hazers.hazers.forEach(h => { if(body.hazer === "all" || h.name === body.hazer){
        if(typeof body.haze === "number") h.haze_pct = body.haze; if(typeof body.fan === "number") h.fan_pct = body.fan; } }); return ok("Set.");
      case "/api/remote/group-disarm": return this.groupDisarm(body.group);
      case "/api/remote/arm-hold": {
        // A new hold has no hold_id; each beat after carries the id it was
        // given. Armed after six beats of the same hold, as before.
        const k = body.group, g = s.flames.groups[k], needs = (s.arming || {}).needs_s || 1.0;
        if(!g) return {code: 400, body: {error: "That is not a flame group flamesafe reports."}};
        this.holds = this.holds || {}; this.holdIds = this.holdIds || 0;
        const h = this.holds[k];
        if(body.hold_id === undefined || body.hold_id === null){
          if(g.armed === "armed" || g.wanted) return {code: 409, body: {error: g.name + " is already armed or asked for.", let_go: true}};
          const id = ++this.holdIds;
          this.holds[k] = {id: id, beats: 1};
          return {code: 200, body: {ok: true, hold_id: id, held_s: 0.0, needs_s: needs}};
        }
        if(!h || h.id !== body.hold_id) return {code: 409, body: {error: "The hold was interrupted. Lift your finger and hold again.", let_go: true}};
        h.beats += 1;
        if(h.beats >= 6){
          g.armed = "armed"; g.wanted = true; delete this.holds[k];
          return {code: 200, body: {ok: true, armed: true, hold_id: h.id, text: g.name + " is asked for: the Stream Deck took the hold and set it."}};
        }
        return {code: 200, body: {ok: true, hold_id: h.id, held_s: Math.round((h.beats - 1) * 100) / 1000, needs_s: needs}};
      }
      case "/api/remote/arm-release": this.dropHolds(body.group); return ok();
      case "/api/remote/alert": s.alerts = s.alerts.filter(a => a.id !== body.id); return ok("Acknowledged.");
      case "/api/remote/announce-play": { const it = s.announce.items.find(i => i.id === body.id);
        s.announce.playing = {id: body.id, name: it ? it.name : body.id, elapsed_s: 0, length_s: it ? it.length_s : 10, held_schedule: sch.state !== "SHOW"};
        if(sch.state !== "SHOW") sch.held = true; return ok("Playing."); }
      case "/api/remote/preroll-continue": if(body.phase === "done"){ s.preroll.continued = {by: s.me.user, off: ["video"], failed: "Video"}; delete s.preroll.show_check; s.alert_hold = {held: false}; s.alerts = []; } return ok("Continuing.");
      case "/api/remote/cleaning-stop": s.cleaning = {available: true, state: "idle", text: ""}; return ok("Stopped.");
      case "/api/remote/purge-stop": if(s.morning) s.morning.timed = null; return ok("Timed purge stopped: everything it fired is back to zero.");
      case "/api/remote/announce-stop": s.announce.playing = null; return ok("Stopped.");
      case "/api/remote/laser-arm": (s.laser_arm.lasers || []).forEach(l => { if(body.all || (body.lasers || []).includes(l.id)) l.armed = !!body.on; }); return ok("Done.");
      default: return ok();
    }
  }
  async attach(page){
    await page.route("**/*", async route => {
      const req = route.request();
      const u = new URL(req.url());
      const p = u.pathname;
      if(req.method() === "POST"){
        let body = {};
        try{ body = JSON.parse(req.postData() || "{}"); }catch(e){}
        const r = this.onPost(p, body);
        return route.fulfill({status: r.code, contentType: "application/json", body: JSON.stringify(r.body)});
      }
      if(p === "/" || p === "/remote" || p === "/remote1") return route.fulfill({status: 200, contentType: "text/html; charset=utf-8", body: this.html});
      if(p === "/remote2") return route.fulfill({status: 200, contentType: "text/html; charset=utf-8", body: this.html2});
      if(p === "/remote-logic.js") return route.fulfill({status: 200, contentType: "application/javascript; charset=utf-8", body: this.logic});
      if(p === "/api/remote/ui-default") return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify({default: this.uiDefault})});
      if(p.startsWith("/brand/") && process.env.NOFONT) return route.fulfill({status: 404, body: "{}"});
      if(p.startsWith("/brand/")){
        const f = path.join(REPO, "ltcplay", "web", "brand", path.basename(p));
        if(fs.existsSync(f)) return route.fulfill({status: 200, body: fs.readFileSync(f),
          contentType: f.endsWith(".png") ? "image/png" : f.endsWith(".woff2") ? "font/woff2" : f.endsWith(".svg") ? "image/svg+xml" : "application/octet-stream"});
        return route.fulfill({status: 404, body: "{}"});
      }
      if(p === "/api/remote/status"){
        if(this.dead || this.name === "stale" && this.served > 3) return route.abort();
        this.served = (this.served || 0) + 1;
        const st = clone(this.st);
        st.served_at = Date.now();
        return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(st)});
      }
      const r = routesFor(this.name, this.st);
      if(p in r) return route.fulfill({status: 200, contentType: "application/json", body: JSON.stringify(r[p])});
      return route.fulfill({status: 200, contentType: "application/json", body: "{}"});
    });
  }
}
module.exports = {FakeEngine, SCENARIOS};
