
// LOGIC BEGIN. Pure functions, no page access: the selftest runs these in
// node to prove the stale banner and the disabled controls.
function isStale(nowMs, lastOkMs, freshS){
  if(lastOkMs === null || lastOkMs === undefined) return true;
  return (nowMs - lastOkMs) > freshS * 1000;
}
// Which controls may be pressed. Hold and disarm only take risk
// away, so they are never held back by a stale page; everything that acts
// on what the page shows needs fresh state.
function controlStates(st, stale){
  const sch = (st && st.schedule) || {};
  const tr = (st && st.transport) || {};
  const fresh = !stale && !!st;
  return {
    hold: true, disarm: true,
    startNow: fresh && !!sch.attached,
    resume: fresh && !!sch.attached,
    transport: fresh && !!tr.programming
  };
}
function lampClass(g, flames){
  if(!flames || flames.stale || !flames.connected) return "unknown";
  if(g.armed === "armed") return "armed";
  if(g.armed === "held") return "held";
  if(g.armed === "disarmed") return "disarmed";
  return "unknown";
}
// What the page says while arming from a screen is switched off (the
// default; remote.py ARMING_OFF says the same).
const ARMING_OFF = "Arming from a screen is switched off on this show PC. To turn it on: on the show machine itself, Programming, Arming from a screen, Turn on. Until then arm from the Stream Deck.";
// May this page start or carry on a hold to arm group i? Only with
// arming switched on, a status heard within the arming
// freshness (1 s), flamesafe's real state reported within 1 s, and the
// group not already armed or asked for.
// Signed out is view only (2026-10-28): what the engine's whoami says
// this screen may do (st.me.may), the same answer it gives a press. A row
// the engine does not mention (no users) may. Stop, Hold,
// Acknowledge and every disarm are never asked.
const SIGN_IN_WORDS = "Sign in to use controls. Stop, Hold, Acknowledge and every disarm still work.";
function mayRow(st, perm){
  const m = st && st.me && st.me.may;
  return !(m && m[perm] === false);
}
function armState(st, nowMs, lastOkMs, i){
  const a = (st && st.arming) || {};
  const fl = (st && st.flames) || {};
  const g = (fl.groups || [])[i];
  if(!mayRow(st, "arm")) return {ok:false, signin:true, why:SIGN_IN_WORDS};
  if(!a.enabled) return {ok:false, off:true, why:ARMING_OFF};
  if(isStale(nowMs, lastOkMs, a.fresh_s || 1.0)) return {ok:false, why:"Not live: no contact with the engine in the last second."};
  if(!fl.connected || fl.stale || fl.age_ms === null || fl.age_ms === undefined || fl.age_ms > 1000)
    return {ok:false, why:"flamesafe's real state is not known."};
  if(!g) return {ok:false, why:"No such group."};
  if(g.armed === "armed" || g.wanted) return {ok:false, why:"Already armed or asked for."};
  return {ok:true, why:""};
}
function staleText(nowMs, lastOkMs, drawErr){
  if(drawErr){
    const d = (lastOkMs === null || lastOkMs === undefined) ? "never" :
      Math.max(0, (nowMs - lastOkMs) / 1000).toFixed(1) + " s ago";
    return "THIS PAGE CANNOT SHOW the engine's status (" + drawErr + "). Last shown: " + d +
      ". The show carries on by itself. Start now, Resume and the programming controls are off until it can.";
  }
  if(lastOkMs === null || lastOkMs === undefined)
    return "NOT CONNECTED to the show engine yet.";
  const s = Math.max(0, (nowMs - lastOkMs) / 1000);
  return "CONNECTION LOST: last heard from the engine " + s.toFixed(1) +
    " s ago. The show carries on by itself. Start now, Resume " +
    "and the programming controls are off until it is back.";
}
// What the countdown reads: the engine's own figure (ST.countdown),
// counted down on the page while it is running, and not at all while a
// show is held (its clock is frozen, so is the time left). null shows
// nothing: a show of unknown length is never guessed.
//
// The countdown the screen draws (soak 2026-10-21: the seconds digit
// flickered between two values before it counted down). Each poll used to
// restart the count from the engine's figure, aged from when it arrived,
// so a figure that was rounded down or a reply that came a little late put
// the digit back up by one. Now the page keeps ONE moment it counts to, on
// its own monotonic clock (performance.now, never the PC's wall clock), set
// from each figure as it arrives: a new figure may bring that moment
// earlier, never later, unless it moved later by more than CD_SLACK_MS (a
// show delayed, the next show changed). A held show's figure is frozen.
const CD_SLACK_MS = 1500;
function countdownAnchor(prev, c, monoMs){
  if(!c || typeof c.s !== "number" || !isFinite(c.s)) return null;
  const s = Math.max(0, c.s);
  if(!c.running) return {kind:c.kind, label:c.label, running:false, s:s, at:null};
  let at = monoMs + s * 1000;
  if(prev && prev.running && prev.kind === c.kind && prev.label === c.label &&
     at <= prev.at + CD_SLACK_MS)
    at = Math.min(prev.at, at);
  return {kind:c.kind, label:c.label, running:true, s:s, at:at};
}
function countdownRead(a, monoMs){
  if(!a) return null;
  const s = a.running ? Math.max(0, (a.at - monoMs) / 1000) : a.s;
  return {kind:a.kind, label:a.label, s:s};
}
// Morning and Shutdown: what a machine tile reads, from the engine's view
// of the page (MO), flamesafe's lamps (ST.flames) and the purge state.
// Display only: the engine decides what fires.
function machineLook(m, mo, flames, purge){
  const locked = !!(mo && mo.lockout && mo.lockout.machines && mo.lockout.machines[m.id]);
  if(locked) return {cls:"locked", word:"Locked out"};
  const sel = !!(purge && (purge.selected || []).includes(m.id));
  // A waver with no pan or tilt slot (a default wave flamer) is never
  // purged by the engine; its notes say why, in one line.
  if(m.kind === "waver" && (m.pan == null || m.tilt == null))
    return {cls:sel ? "sel" : "", word:"Never purged: no pan, tilt and fire map yet"};
  // A fireball is never purged (its channels are a sequence); the engine
  // refuses a click on it.
  if(m.kind === "fireball") return {cls:"", word:"Never purged: a fireball has no purge sequence yet"};
  // Anything else the engine never purges (an unknown multi-channel group
  // until it is declared), in the engine's own words.
  if(m.never_purged) return {cls:sel ? "sel" : "", word:"Never purged: " + m.never_purged};
  if(!sel) return {cls:"", word:"Off"};
  const g = ((flames && flames.groups) || []).find(x => x.name === m.group);
  const armed = !!(g && g.armed === "armed" && flames && !flames.stale);
  if(purge.firing && armed) return {cls:"firing", word:"FIRING"};
  if(armed) return {cls:"armed", word:"Purge armed"};
  return {cls:"sel", word:(g && g.reason) ? "Held: " + g.reason : "Arming..."};
}
// The re-arm wait (Jeff, 2026-10-26: a quick re-arm after a Disarm sat on
// "arming" with no word why). How long before group `g` may be armed
// again, in ms: flamesafe's re-arm dwell (dwell_s, whole seconds rounded
// up, as of its frame `flAgeMs` old) and the Stream Deck's re-arm
// refractory window (rearm_ms, as of the status), less the status's age
// on this page. Rounded up, never down: the page never says ready early.
// Words only: the deck and flamesafe decide, and a hold is still needed.
function rearmLeftMs(g, pageAgeMs, flAgeMs){
  if(!g || g.armed === "armed") return 0;
  const age = Math.max(0, Number(pageAgeMs) || 0);
  const dwell = (Number(g.dwell_s) || 0) * 1000 - Math.max(0, Number(flAgeMs) || 0) - age;
  const deck = (Number(g.rearm_ms) || 0) - age;
  return Math.max(0, dwell, deck);
}
function rearmText(ms){ return "Re-arm in " + (Math.ceil(ms / 100) / 10).toFixed(1) + " s"; }
const REARM_READY = "ready: hold to arm";
const REARM_REFUSED = "Refused: the re-arm wait was not over. Hold again.";
const REARM_REFUSED_PURGE = "Refused: the re-arm wait was not over. Press Clear, then click it again.";
// The arm button's small line about the wait, or null to leave armHint's:
// `left` ms of wait, `waited` a wait seen in the last 15 s, `ok` armState's
// verdict. (There is no Abort latch any more, 2026-10-30.)
function armWaitWords(left, waited, ok){
  if(left > 0) return rearmText(left);
  if(ok && waited) return REARM_READY;
  return null;
}
// What a hold that ended in an error says, or null for the engine's own
// sentence: the re-arm wait (code refractory, or no code while a wait
// still runs).
function holdRefusalWords(code, waitLeftMs){
  if(code === "refractory" || (!code && waitLeftMs > 0)) return REARM_REFUSED;
  return null;
}
// What a selected machine's tile says under its word while it is not
// armed: the deck refused its purge arm, the wait still running, or
// nothing.
function machineWait(m, flames, purge, pageAgeMs){
  const sel = !!(purge && (purge.selected || []).includes(m.id));
  const g = ((flames && flames.groups) || []).find(x => x.name === m.group);
  if(!sel || !g || g.armed === "armed" || (flames && flames.stale)) return "";
  // A default wave flamer is never purged (machineLook says so): no wait.
  if(m.kind === "waver" && (m.pan == null || m.tilt == null)) return "";
  if(g.purge_refused === "refractory") return REARM_REFUSED_PURGE;
  if(g.purge_refused === "screenonly") return "Not armed: it has no Stream Deck key, so only its Hold to arm button arms it.";
  const left = rearmLeftMs(g, pageAgeMs, flames && flames.age_ms);
  return left > 0 ? rearmText(left) : "";
}
// The fire key's own rule (review of PR #68, P1-4): the heartbeats are not
// the key; the browser's key repeat events are. A press older than graceMs
// whose last repeat event (or its start) is more than gapMs ago has lost
// its key: a lost keyup, a browser that stopped delivering events, or an
// OS with key repeat off. true ends the press. A physically stuck key
// still repeats, so this is not a dead-man switch; the Stream Deck's FIRE
// key is.
function fireKeyLapsed(nowMs, startAt, repeatAt, graceMs, gapMs){
  return nowMs - startAt > graceMs && nowMs - Math.max(repeatAt || 0, startAt) > gapMs;
}
function clockText(sec){
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, x = sec % 60;
  const two = (n) => String(n).padStart(2, "0");
  return (h ? h + ":" + two(m) : String(m)) + ":" + two(x);
}
// Which MODE LTC Player is in, in one word and one sentence, from the
// engine's own state: SHOW while a show file runs for real, PROGRAMMING
// while a rehearsal runs (the engine's transport.programming), NOT RUNNING
// otherwise. Never guessed from the screen that is open.
function modeLook(st){
  const show = (st && st.show) || {};
  const tr = (st && st.transport) || {};
  if(!st) return {cls:"", word:"...", text:"Waiting for the engine."};
  if(show.running && tr.programming)
    return {cls:"prog", word:"Programming", text:"A rehearsal is running: the scrubbing controls work, and the scheduler starts no show. To go back to Show: Stop, choose Show on the Show file card, then Run."};
  if(show.running)
    return {cls:"show", word:"Show", text:"Show mode: a show file is running and the scheduler starts each show at its time. Scrubbing is off."};
  return {cls:"", word:"Not running", text:"No show file is running. Nothing reaches the rig until Run is pressed on the Programming screen, in Show or in Programming."};
}
// One engine check's look on the Morning page: its class and the word
// before its sentence. An unknown level is never shown as fine.
function checkLook(level){
  if(level === "ok") return {cls:"ok", word:"OK"};
  if(level === "bad") return {cls:"bad", word:"FAULT"};
  if(level === "warn") return {cls:"warn", word:"Check"};
  return {cls:"", word:"Not known"};
}
// The laser arm (Jeff, 2026-10-22; laserarm.py). One line that says where
// the lasers stand: who is armed, and whether BEYOND's laser output is on.
function laserArmLine(la){
  const ls = la.lasers || [];
  const armed = ls.filter(x => x.armed);
  if(!ls.length) return "No lasers are configured.";
  if(!armed.length) return "Lasers: DISARMED, every laser. BEYOND's laser output is off." + (la.output_fault ? " Arming is off: " + la.output_fault + "." : "");
  if(la.output_permitted) return "Lasers: ALL ARMED. BEYOND's laser output is on; the show lights them.";
  return "Lasers: " + armed.map(x => x.name).join(", ") + " ARMED (" + armed.length + " of " + ls.length + "). BEYOND's laser output stays OFF: " + (la.why_off || "not every laser is armed") + ".";
}
// The banners above the show screen, folded (Jeff, 2026-10-24: two FAULT
// banners and a flame note squashed the flame groups). `items` are the
// banners showing, each {key, fault, label, text}, in the order drawn. One
// banner is shown whole; two or more fold into one line: red when any is a
// fault, every fault named by its label, the worst one's sentence (the
// first fault, else the first), opened with Show all. Folding hides
// nothing for good: every banner, and every Acknowledge, is one tap away.
// `seen` is the set of fault keys drawn before (null on the first draw):
// a fault key not in it is `fresh` (review of #102: a new fault must not
// just bump a count), and `keys` is what to remember for the next draw.
function alertFold(items, seen){
  const list = (items || []).filter(i => i && i.text);
  const keys = list.filter(i => i.fault).map(i => String(i.key));
  const fresh = !!seen && keys.some(k => !seen.has(k));
  if(list.length < 2) return {fold:false, n:list.length, fresh:fresh, keys:keys};
  const worst = list.find(i => i.fault) || list[0];
  const faults = list.filter(i => i.fault).map(i => i.label || "Fault");
  return {fold:true, n:list.length, fault:faults.length > 0,
    kind: !faults.length ? "Note" :
      (faults.length === 1 ? "Fault: " : faults.length + " faults: ") + faults.join(", "),
    text: worst.text,
    more: list.length - 1, fresh:fresh, keys:keys};
}
// flamesafe multi-safety (2026-10-26): the count line on the card of a
// group with more than one safety slot, one per fixture. A locked safety
// stays at zero and is left out of the arm. Empty for a one-safety group,
// and while the state is not known.
function safetyLine(g, c){
  const up = g.safeties_up || [], lk = g.safeties_locked || [];
  if(up.length < 2 || c === "unknown") return "";
  const locked = lk.filter(Boolean).length;
  const n = up.filter((u, i) => u && !lk[i]).length;
  const of = up.length - locked;
  return g.name + ": " + n + " of " + of + (locked ? " unlocked" : "") +
    " safeties up" + (locked ? ", " + locked + " locked out" : "") +
    (c === "armed" ? "; ARMED" : "; NOT armed");
}
// "armed n of m machines" (Jeff, 2026-10-26), on every flame group's card:
// m is the group's machines on the Startup page's list that are not
// locked out (those are said apart, "k locked out"), n how many of them
// flamesafe has armed: a one-safety group arms all its machines together,
// so it reads 0 of m or m of m. A group with several safeties keeps its
// own count (safetyLine). "" when the list, or the group's state, is not
// known: the card then reads as before. `gm` is the status's
// morning.group_machines.
// A machine is counted out as locked only once flamesafe holds its lock
// (`lock` "ok"); before that it is "k lock pending", and with the lockout
// not trusted every machine is counted and the line says so (second
// review of #122, P2-3).
function machineCountLine(g, c, gm){
  if(!g || c === "unknown" || (g.safeties_up || []).length >= 2) return "";
  const x = gm && gm[g.name];
  if(!x || typeof x.machines !== "number") return "";
  const k = Math.max(0, Number(x.locked) || 0);
  const lk = x.lock || "ok";
  const locked = lk === "ok" ? k : 0;
  const m = Math.max(0, x.machines - locked);
  const n = c === "armed" ? m : 0;
  return "armed " + n + " of " + m + (m === 1 ? " machine" : " machines") +
    (lk === "untrusted" ? "; lockout not trusted" : k ? (lk === "ok" ? ", " + k + " locked out" : ", " + k + " lock pending") : "");
}
// The aim line (2026-10-28), read only, on the card of a group with aim
// slots (the wave flamer's angle): which slots, whether they are loaded
// (flamesafe and LTC Player have the same list), the group's state (ARMED,
// held, disarmed),
// and what was asked for and sent on them in flamesafe's last frame. ""
// for a group without aim slots. `g.aim` is the engine's aim_view.
function aimLine(g, c){
  const a = g && g.aim;
  if(!a || !(a.slots || []).length) return "";
  const j = v => (v && v.length) ? v.join(", ") : "none";
  let t = "Aim " + j(a.slots) + ": " + (a.loaded ? "loaded" :
    a.flamesafe === null || a.flamesafe === undefined ? "flamesafe does not say" :
    "NOT loaded (flamesafe " + j(a.flamesafe) + ", LTC Player " + (a.ltcplay ? j(a.ltcplay) : "not known") + ")");
  t += c === "armed" ? "; ARMED" : c === "held" ? "; held" : c === "unknown" ? "; armed not known" : "; disarmed";
  if(c !== "unknown" && a.asked && a.sent) t += "; asked " + j(a.asked) + ", sent " + j(a.sent);
  return t;
}
// The no-show-file line (Jeff, 2026-10-26; review of #113, P2-a and b):
// what works NOW without a show file, from the status, never a promise.
// `su` is the engine's setup view, `st` the status. "" when a show file
// loads.
function noFileLine(su, st){
  if(!su || su.show_files !== 0) return "";
  const errs = su.show_file_errors || [];
  let t = errs.length ? "No show file loads yet (" + errs.join("; ") + ")." : "No show file yet.";
  const fl = (st && st.flames) || {};
  const live = !!(fl.connected && !fl.stale && (fl.groups || []).length);
  const on = !!(st && st.arming && st.arming.enabled);
  if(live && on) t += " Arming and disarming flames still work";
  else if(live) t += " Disarming flames still works; arming from this screen is switched off (Programming, Arming from a screen), so arm from the Stream Deck";
  else t += " flamesafe is not reporting, so no flame group can be armed from here; Disarm all and Stop still work";
  return t + ". Live flame programming needs xlights_networks.xml in the show folder; playing a show needs a show file.";
}
// How far the window reaches below the screen's work area, in CSS px: the
// room the page keeps under itself (Jeff, 2026-10-26). `scr` is
// window.screen, `win` the window (screenY, outerHeight), `minPad` the
// supervisor's pad_bottom. Only an overlap that lies in the screen's own
// band below its work area (the taskbar's, screen.height less the work
// area) counts, give or take a window border: a screen that reports less
// than the window (a headless browser, a stale screen) adds nothing. An
// overlap under 16 px is a maximized window's own border. Never over
// PAD_MAX, the supervisor's floor included (review of #115, P2-2: the
// room kept at the bottom must never eat the screen).
const PAD_MAX = 160;
function bottomPad(scr, win, minPad){
  const floor = Math.max(0, Math.min(PAD_MAX, Number(minPad) || 0));
  const ah = Number(scr && scr.availHeight) || 0, oh = Number(win && win.outerHeight) || 0;
  const sh = Number(scr && scr.height) || 0;
  if(!ah || !oh || !sh) return floor;
  const workBottom = (Number(scr.availTop) || 0) + ah;
  const band = sh - workBottom;            // the taskbar's band, in CSS px
  const over = (Number(win.screenY) || 0) + oh - workBottom;
  if(over < 16 || band <= 0 || over > band + 32) return floor;
  return Math.max(floor, Math.min(PAD_MAX, Math.round(over)));
}
// ONE BUTTON PER FLAME GROUP (Jeff, 2026-10-26). What a group's button
// does: "hold" (Hold to arm, the hold exactly as before), "disarm" (one
// press disarms, as the old Disarm did) or "lamp" (arming from a screen
// is off: the button only shows the state, greyed, and there is no
// per-group disarm, as before). Armed or asked for is Disarm; so is a
// state not known (a disarm only takes risk away). `c` is lampClass's.
function armButtonMode(g, c, on){
  if(!on) return "lamp";
  if(c === "unknown") return "disarm";
  if(g && (g.armed === "armed" || g.wanted)) return "disarm";
  return "hold";
}
// The Disarm button's small line, in words.
function disarmHint(c, held){
  if(held === "cycle") return "Disarm, then hold";
  return "One press disarms";
}
// What a click on a two-way button does: "disarm" or nothing. The mode
// the button had when the finger went DOWN decides, never the mode it
// turned into under the finger: a hold that has just armed a group turns
// its button into Disarm, and lifting that finger must never disarm it.
// A keyboard press (Enter or Space, no finger) goes by the mode now.
function clickAction(downMode, nowMode, keyboard){
  if(keyboard) return nowMode === "disarm" ? "disarm" : "";
  return downMode === "disarm" ? "disarm" : "";
}
// The flame block's two-way button: "arm" (Arm all flame groups, a hold)
// only while screen arming is on, flamesafe's real state is known and
// every group is off; "disarm" (Disarm all flame groups, one press)
// otherwise: any group armed or asked for, a mix, a state not known.
function allButtonMode(st, stale){
  const fl = (st && st.flames) || {}, gs = fl.groups || [];
  if(stale || !st || !st.arming || !st.arming.enabled) return "disarm";
  if(!fl.connected || fl.stale || !gs.length) return "disarm";
  if(gs.some(g => g.wanted || (g.armed !== "disarmed" && g.armed !== "held"))) return "disarm";
  return "arm";
}
const ONE_GROUP_WORDS = "Live flame programming is on, and it allows one flame group at a time. Hold one group's own button instead.";
const LF_UNKNOWN_WORDS = "Live flame programming's state could not be read, so Arm all arms nothing. Hold one group's own button instead.";
const KEY_HOLD_WORDS = "Arming is a hold: press and hold with your finger or mouse.";
// Why Arm all may hold nothing at all right now, or "": live flame
// programming on with one group at a time, or its
// state not readable. Checked when the hold starts AND on every heartbeat
// (review of #118, P2-1): turned on mid-hold, it stops the hold.
function armAllRefusal(st){
  const lf = (st && st.live_flame) || {};
  if(lf.error) return LF_UNKNOWN_WORDS;
  if(lf.on === true && lf.one_group_at_a_time !== false) return ONE_GROUP_WORDS;
  return "";
}
// Arm all: which groups one hold on it holds to arm (`go`), and which stay
// out and why (`out`, each {group, name, why}). The same rules as a
// group's own button (armState, the re-arm wait `waits[i]` ms) for every
// group; nothing at all with live flame programming on one group at a
// time. The engine still checks every
// heartbeat of every group's hold, as it does for a group's own button.
function armAllPlan(st, nowMs, lastOkMs, waits){
  const gs = ((st && st.flames) || {}).groups || [];
  const no = armAllRefusal(st);
  if(no) return {go:[], out:[], why:no};
  const go = [], out = [];
  gs.forEach((g, i) => {
    const as = armState(st, nowMs, lastOkMs, i);
    const w = Number((waits || {})[i]) || 0;
    if(!as.ok) out.push({group:i, name:g.name, why:as.why});
    else if(w > 0) out.push({group:i, name:g.name, why:rearmText(w)});
    else go.push(i);
  });
  return {go:go, out:out, why: go.length ? "" : (gs.length ? "no flame group can be armed right now" : "flamesafe reports no flame groups")};
}
// What a hold on Arm all says when it ends: the groups asked for, then
// every group that stayed out, by name, with why (one reason said once).
function armAllWords(parts, out, names){
  const nm = i => (names && names[i]) || ("group " + (i + 1));
  const asked = (parts || []).filter(p => p.end === "armed").map(p => nm(p.group));
  const left = (parts || []).filter(p => p.end !== "armed").map(p => ({name:nm(p.group), why:p.why || "the hold ended"}))
    .concat((out || []).map(o => ({name:o.name || nm(o.group), why:o.why})));
  let t = asked.length ? "Arm all: " + asked.join(", ") + (asked.length === 1 ? " is" : " are") + " asked for; the lamps show when armed." : "Arm all: nothing was armed.";
  const by = [];
  left.forEach(l => { const why = String(l.why || "").replace(/[.\s]+$/, ""); const b = by.find(x => x.why === why); if(b) b.names.push(l.name); else by.push({why:why, names:[l.name]}); });
  by.forEach(b => { t += " Not armed: " + b.names.join(", ") + " (" + b.why + ")."; });
  return t;
}
// Why an unlock of machine `m` ({name, group}) is refused on this page, or
// "": its flame group armed or asked for, or flamesafe's state not known
// (review of #122, P1-1). A lock is always allowed.
function unlockRefusal(m, flames){
  const fl = flames || {};
  if(!fl.connected || fl.stale) return "flamesafe's armed groups are not known right now, so " + m.name + " stays locked out. Unlock it once the lamps are live.";
  const g = (fl.groups || []).find(x => x.name === m.group);
  if(g && (g.armed === "armed" || g.wanted)) return "Disarm " + m.group + " first, then unlock " + m.name + ".";
  return "";
}
// STOP is a double tap (Jeff, 2026-10-30: no pop-up). `prev` is the first
// tap ({at, kind}, on the page's monotonic clock) or null. A tap of the
// same kind ("touch", "mouse", "pen", or "key" for Enter or Space) within
// STOP_TAP_MS fires, however fast (a panic double tap must stop; only a
// held key's repeats are kept out, by the keydown guard), or at any time
// while `enlarged`. Anything else is a first tap. Two fingers are both
// "touch". 2.0 runs these very functions, and its stop-core script keeps
// a copy (stopTapStep2, stopTapKind2) that the selftest keeps the same.
// 3 s (the Windows stress test, 2026-11-09: "Stop does nothing" twice,
// a second tap that came too late); the button says "tap twice" and
// counts the window down.
const STOP_TAP_MS = 3000;
// A lone tap that timed out: the message line's words, for this long.
const STOP_MISSED_WORDS = "Stop not sent: tap twice";
const STOP_MISSED_MS = 2000;
// The first tap's whole seconds left, never 0: "to stop (3)".
function stopLeftS(leftMs){
  return Math.max(1, Math.ceil(Math.max(0, leftMs) / 1000));
}
// The Stop show button's two lines, [big, small]: "Stop show" / "tap
// twice"; armed, "Tap again" / "to stop (3)"; a lone tap that timed out,
// "Not sent" / "tap twice" (the message line says "Stop not sent: tap
// twice"). Two lines, at full size, so the words fit a portrait iPad's
// button and the strip's.
function stopLines(armedLeftMs, missed){
  if(armedLeftMs !== null && armedLeftMs !== undefined) return ["Tap again", "to stop (" + stopLeftS(armedLeftMs) + ")"];
  if(missed) return ["Not sent", "tap twice"];
  return ["Stop show", "tap twice"];
}
function stopTapStep(prev, nowMs, kind, enlarged){
  if(prev && prev.kind === kind && nowMs >= prev.at){
    if(nowMs - prev.at <= STOP_TAP_MS || enlarged) return {fire:true, next:null};
  }
  return {fire:false, next:{at:nowMs, kind:kind}};
}
function stopTapKind(e){
  if(e && e.pointerType) return e.pointerType;
  return e && e.detail === 0 ? "key" : "mouse";
}
// ONE laser pill (Jeff, 2026-10-26). The engine sends two chips, "Laser
// arm" (who is armed) and "Lasers" (what the show last sent BEYOND, or a
// fault: LINK LOST, blanking not set up). The page shows them as one pill
// with one sentence: the fault first, then the arm, then BEYOND's laser
// output, e.g. "all disarmed, output dark". Its colour is the worse of
// the two; every fact either said is in its tooltip. `la` is the status's
// laser_arm view, `bl` its beyond_link. Display only.
const CHIP_RANK = {bad:3, warn:2, ok:1, off:0};
function laserPill(subs, la, bl){
  subs = subs || [];
  const arm = subs.find(x => x && x.name === "Laser arm");
  const out = subs.find(x => x && x.name === "Lasers");
  if(!arm || !out) return subs;
  const ls = (la && !la.error && la.lasers) || null;
  const armed = ls ? ls.filter(x => x.armed) : [];
  let a, o;
  if(!ls || arm.state === "bad") a = "arm " + String(arm.word || "not known");
  else if(!ls.length) a = "none set up";
  else if(!armed.length) a = "all disarmed";
  else if(armed.length === ls.length) a = "all armed";
  else a = armed.length + " of " + ls.length + " armed";
  if(ls && la.output_permitted) o = "output allowed";
  else if(ls && !armed.length) o = "output dark";
  else if(ls) o = "output off";
  else o = "output not known";
  const fault = out.state === "bad" ? String(out.word || "fault") + ": " : "";
  // A warning from what the show last sent ("lit? link unknown", "BEYOND
  // restarted: dark until lit again") is said in the sentence too, never
  // only in colour and a tooltip a touch screen cannot show (review of
  // #122, P2-4).
  const warn = out.state === "warn" || (out.state === "ok" && /\blit\b/.test(String(out.word || ""))) ? "; " + String(out.word || "check") : "";
  const state = (CHIP_RANK[arm.state] || 0) >= (CHIP_RANK[out.state] || 0) ? arm.state : out.state;
  const title = ["Lasers: " + fault + a + ", " + o + warn + ".",
    "Laser arm: " + arm.word + (arm.title ? " (" + arm.title + ")" : "") + ".",
    "What the show last sent BEYOND: " + out.word + (out.title ? " (" + out.title + ")" : "") + ".",
    armed.length ? "Armed: " + armed.map(x => x.name).join(", ") + "." : "",
    ls && la.why_off && armed.length ? "BEYOND's laser output stays off: " + la.why_off + "." : "",
    la && la.arm_here ? "Arming from here is off: " + la.arm_here + "." : "",
    la && la.arm_refusal ? "An arm now would be refused: " + la.arm_refusal + "." : "",
    la && la.error ? "The laser arm could not be read: " + la.error + "." : "",
    bl && bl.link === "lost" && bl.why ? "BEYOND: " + bl.why + "." : ""].filter(Boolean).join(" ");
  const pill = {name:"Lasers", state:state, word:fault + a + ", " + o + warn, title:title, pill:"lasers"};
  const i = Math.min(subs.indexOf(arm), subs.indexOf(out));
  const rest = subs.filter(x => x !== arm && x !== out);
  rest.splice(i, 0, pill);
  return rest;
}
// WEATHER (weather.py, WEATHER.md): what the Weather block draws, from the
// engine's /api/remote/weather (`w`) and how many seconds ago this page
// last heard it. The engine writes each line, with when it was last
// updated (a date and time in the show's zone, never "min ago"); a reply
// older than w.page_dead_s greys the whole block, so no number is ever
// shown once this page has stopped hearing the engine.
function wxBlock(w, heardAgoS){
  const dead = (w && Number(w.page_dead_s)) || 60;
  const one = (cls, text) => ({cls:cls, lines:[{cls:"none", text:text}]});
  if(!w || heardAgoS === null || heardAgoS === undefined) return one("none", "Wind: not heard from the engine yet.");
  if(heardAgoS > dead) return one("none", "NO CURRENT DATA, use on-site wind check. This screen has not heard from the engine for " + Math.round(heardAgoS) + " s.");
  if(w.state === "error") return one("none", "Weather is off: " + (w.error || "its settings are wrong."));
  if(w.state === "off" || w.enabled === false) return one("off", w.text || "Weather is switched off in ltcplay_remote.json.");
  if(!w.state || !Array.isArray(w.lines)) return one("none", "Wind: the engine has not said yet.");
  const L = [];
  if(w.internet) L.push({cls:"net", text:w.internet});
  (w.banner || []).forEach(t => L.push({cls:"alert", text:t}));
  if(w.disagree) L.push({cls:"alert", text:w.disagree});
  w.lines.forEach(l => {
    // "backup": a reading from a backup station, amber but not STALE.
    const lv = l.level === "ok" || l.level === "stale" ? l.level : l.level === "backup" ? "backup" : "none";
    L.push({cls:lv === "backup" ? "stale" : lv, text:(lv === "stale" ? "STALE. " : "") + l.text});
  });
  if(w.alerts_line) L.push({cls:w.alerts_line.level === "ok" ? "dim" : "none", text:w.alerts_line.text});
  return {cls:["ok", "stale", "none"].includes(w.state) ? w.state : "none", lines:L};
}
// The Users screen's "sign everyone out after the last show" minutes, as
// typed: a whole number 0 to 1440, or null (blank, a word, a decimal,
// out of range). A null is said on the screen and nothing is saved, so a
// blank field never switches it off without a word (review of PR #125,
// P2-7).
function lastShowMinutes(text){
  const t = String(text == null ? "" : text).trim();
  if(!/^[0-9]{1,4}$/.test(t)) return null;
  const n = parseInt(t, 10);
  return n <= 1440 ? n : null;
}
// What the Rack screen says after Stop: /api/stop also disarms every flame
// group (review of #126, P2-1), and a disarm that failed is said plainly,
// never a bare "Stopped." (third review of #126, P2-2).
const STOP_DISARM_FAILED = "Stopped, but the flame disarm FAILED: use Disarm all flame groups or the hardware e-stop.";
// Stop is the Stream Deck's Stop, a complete halt (Jeff, 2026-10-28: "no
// way to simply stop the show"), with every guarantee the removed Abort
// had (Jeff, 2026-10-30): every flame group and laser disarmed, the fireball, the
// cleaning and Programming playback ended, a live show ended and faded to
// dark, and the schedule put on Hold. Run stays on: Resume releases the
// Hold. Nothing is fired and nothing is latched. Only
// Run off ({run_on:false}) also stops playback.
function stopWords(j){
  const f = j && j.flames;
  if(f && f.disarmed === false) return STOP_DISARM_FAILED;
  const bad = ((j && j.failed) || []).filter(x => x !== "flames");
  if(bad.length)
    return "Stopped, but these did NOT work: " + bad.join(", ") +
      (j && j.error ? " (" + j.error + ")" : "") + ". Use Disarm all flame groups, or the hardware e-stop.";
  const said = [];
  if(j && j.show && j.show.ok) said.push("The show ended and the rig is fading to dark.");
  else said.push("The rig is dark.");
  if(f && f.disarmed === true) said.push("Every flame group was disarmed.");
  if(j && j.lasers && j.lasers.disarmed === true) said.push("Every laser was disarmed.");
  if(j && j.programming && j.programming.ok) said.push("Programming playback stopped.");
  if(j && j.run_on === true){
    if(j.hold && j.hold.ok) said.push("The schedule is on Hold: no show starts until you press Resume. Arm each group again when it is safe.");
    else said.push("Run is still on. Arm each group again when it is safe.");
  }
  else said.push("Run is off. Press Run to bring the rig back.");
  return "Stopped. " + said.join(" ");
}
// Is there anything for Stop to stop (Jeff, 2026-10-30: "if nothing is
// playing there is no Stop button")? A scheduled show live (playing, or
// paused on Hold), the rig check before a show or a Start now waiting for
// the show audio (a show about to play), Programming playback on the show
// audio or flames following Programming, or, with no scheduler in this
// engine, Run playing or starting.
// Every screen draws its Stop only then (and while its own Stop answers).
const TC_MOVING = ["LOCKED", "FREEWHEEL", "FREERUN"];
function stopShows(st){
  const sch = (st && st.schedule) || {}, show = (st && st.show) || {};
  const tr = (st && st.transport) || {}, pr = (st && st.preroll) || {};
  if(sch.state === "SHOW" || sch.state === "PAUSED") return true;
  // The rig is up by the conductor's own look, whatever the scheduler's
  // state says (cold review of #191 round 2): Stop is there.
  if(sch.lit === true) return true;
  if(sch.start_waiting === true) return true;
  if(pr.state === "checking" || pr.state === "queued") return true;
  if(tr.programming && tr.playhead === "show_audio" && tr.playing) return true;
  if(st && st.programming_flames && st.programming_flames.on === true) return true;
  // A rehearsal Run (Programming) with its timecode moving: the rig is
  // playing (cold review of #191, B1).
  if(tr.programming && show.running && TC_MOVING.includes(show.state)) return true;
  return !sch.attached && !!(show.running || show.starting);
}
// A refused press's words: the engine's own sentence, whichever key it
// came in ("error", or "text" for a scheduler press it refused, such as
// Start now with Run off), never a bare "Conflict" (Jeff, 2026-10-28).
function refusedWords(j, statusText){
  const t = j && (j.error || j.text);
  return t ? String(t) : String(statusText || "The engine refused it and did not say why.");
}
// What Stop does, on its button (there is no confirm box: Stop is a double
// tap, Jeff 2026-10-30).
const STOP_TITLE = "Tap twice. Disarms every flame group and every laser, ends the fireball, cleaning and Programming playback, ends a live show and fades it to dark, and puts the schedule on Hold. Nothing is fired. Run stays on: Resume releases the Hold, and each group has to be armed again.";
// Run off (2026-10-30): Stop no longer turns Run off, so the Run card does.
const RUN_OFF_CONFIRM = "Stop everything (as Stop does), then stop playback, so Run is off. No show starts until Run is pressed again.";
// The Show screen's Start now button, or Stop in its place while a show is
// live (Jeff, 2026-10-28: the Play button becomes Stop, same position). A
// live show is the scheduler in SHOW or PAUSED (on Hold), or, with no
// scheduler in this engine, Run playing or starting. Stop is never held
// back by a stale page: it only takes risk away, and it works from every
// screen (review of #144, F7). Before a show, what Start now would be
// refused with (the engine's start_blocked) is on the button; while the
// show audio loads Start now still works and waits for it (F2), and while
// it waits the button reads Loading, starting when ready. Whether Run is
// on is the engine's boolean (show.show_control_on), never its sentence;
// anything but true is off.
function startLook(st, stale, waiting){
  const sch = (st && st.schedule) || {};
  // Stop wherever there is something for it to stop (stopShows): a show
  // live, the rig check, a Start now waiting for the show audio, or
  // Programming playback. Tap twice (Jeff, 2026-10-30).
  if(prerollLook(st && st.preroll).checking)
    return {mode:"stop", label:"Stop", sub:"checking the rig", disabled:false,
            title:"The rig is being checked before the show. Hold or Stop cancels it. " + STOP_TITLE, why:""};
  if(sch.start_waiting && stopShows(st))
    return {mode:"stop", label:"Stop", sub:"starting when ready", disabled:false,
            title:"Start now is waiting for the show audio to load. Hold or Stop cancels it. " + STOP_TITLE, why:""};
  if(stopShows(st))
    return {mode:"stop", label:"Stop", sub:"end the show, rig dark", disabled:false, title:STOP_TITLE, why:""};
  // No contact: what is playing is not known, so Stop is there.
  if(stale && st)
    return {mode:"stop", label:"Stop", sub:"no contact", disabled:false, title:STOP_TITLE, why:""};
  const b = sch.start_blocked;
  const fresh = !stale && !!st && !!sch.attached;
  if(waiting)
    return {mode:"start", label:"Loading", sub:"starting when ready", disabled:true,
            title:"Start now is waiting for the show audio to load. Hold or Stop cancels it.", why:""};
  if(b && b.text && b.loading)
    return {mode:"start", label:"Start now", sub:"audio loading, starts when ready",
            disabled:!fresh, title:String(b.text), why:""};
  if(b && b.text)
    return {mode:"start", label:"Start now", sub:!(st && st.show && st.show.show_control_on === true) ? "Run is off" : "not ready, tap for why",
            disabled:!fresh, title:String(b.text), why:String(b.text)};
  return {mode:"start", label:"Start now", sub:"", disabled:!fresh, title:"", why:""};
}
// The Programming screen's Run button: Stop in its place while Run is in
// force and there is something for Stop to stop (Jeff, 2026-10-28 and
// 2026-10-30; review of #144, F4), one Stop, where Run was. Stop no longer
// turns Run off, so with Run in force and nothing to stop it is Run off
// (behind a confirm), and with Run off it is Run.
function runLook(st, stale){
  const show = (st && st.show) || {};
  // No contact: what is playing is not known, so Stop is there (as on
  // Start now; cold review of #191, B2).
  if(stale && st) return {mode:"stop", label:"Stop", sub:"no contact"};
  if(!(show.running || show.starting)) return {mode:"run", label:"Run", sub:""};
  return stopShows(st) ? {mode:"stop", label:"Stop", sub:"end the show, rig dark"}
                       : {mode:"runoff", label:"Run off", sub:"stop playback"};
}
// What a press on a button that changes between Start and Stop does
// (review of #144, F5): the mode the operator SAW when the finger went
// down; a press whose mode changed under the finger, or within
// FLIP_LOCK_MS of Stop turning back into Start (or Run), does nothing and
// says so. A Stop never turns into a start.
const FLIP_LOCK_MS = 2000;
function pressDecision(seen, now, flippedAt, nowMs, busy){
  if(busy) return {act:"none", say:"Stopping: wait until it has finished."};
  if(seen && seen !== now)
    return {act:"none", say:"The show changed as you pressed (it ended or started), so nothing was done. Look at the screen, then press again."};
  if(now !== "stop" && flippedAt && nowMs - flippedAt < FLIP_LOCK_MS)
    return {act:"none", say:"The show has just stopped. Nothing was done; wait a moment before starting anything."};
  return {act:now, say:""};
}
// The rig check (preroll.py, Jeff 2026-10-28): the state card's word and
// sentence while it runs, the line a failed one leaves on the rack
// screen, and the per-check list.
function prerollLook(pr){
  const p = pr || {};
  const checking = p.state === "checking" || p.state === "queued";
  // An advisory check (required false: Announcements, Jeff 2026-10-30) is a WARNING, never a failure.
  const items = (p.items || []).map(i => String(i.name || "") + (i.ok ? " OK" : (i.required === false ? " WARNING: " : " FAILED: ") + String(i.text || "")));
  const failed = p.state === "failed" && !p.manual && typeof p.age_s === "number" && p.age_s < 600;
  // A passing check with an advisory one not OK still says so on the card's own line.
  const warn = checking ? "" : items.filter(t => / WARNING: /.test(t)).join("; ");
  return {checking:checking, word:"Checking", label:"Checking the rig before " + String(p.label || "the show") + ". Nothing is heard or seen.",
          trouble: failed ? "Rig check FAILED before " + String(p.label || "the show") + ": " + items.filter(t => / FAILED: /.test(t)).join("; ") + "." : "",
          now: checking ? "Checking the rig..." : p.state === "passed" ? (warn ? "Last check passed, with a WARNING" : "Last check passed") : p.state === "failed" ? "Last check FAILED" :
               p.state === "cancelled" ? "Last check was cancelled" : "Not checked yet",
          warn: warn ? warn + "." : "",
          items: items};
}
// After a failed rig check before a show, between shows: the failed items
// and the two choices (check again, or hold to continue anyway), or null.
// After a Continue anyway: what that show runs without.
function prerollChoice(pr, sch){
  const p = pr || {}, s = sch || {};
  if(p.continued && (s.state === "SHOW" || s.state === "PAUSED")){
    const c = p.continued;
    return {continued:true, text:"Continued anyway by " + String(c.by || "someone") + ", late. Left off for this show: " +
      ((c.off || []).join(", ") || "nothing") + ". Failed: " + String(c.failed || "") + "."};
  }
  // A failure a later check cleared (Jeff's field report, 2026-10-29):
  // the failure is gone from every screen; what to press is said.
  if(p.cleared && !p.show_check && s.state !== "SHOW" && s.state !== "PAUSED")
    return {continued:false, cleared:true, text:String(p.cleared.say || "Rig check passed. Press Resume.") +
      " Cleared by " + String(p.cleared.by || "a check") + "; nothing was started."};
  const f = p.show_check;
  if(!f || p.continued) return null;
  if(s.state === "SHOW" || s.state === "PAUSED") return null;
  const bad = (f.items || []).filter(i => i.required !== false && !i.ok).map(i => String(i.name || "") + ": " + String(i.text || ""));
  const off = f.off || [];
  const re = p.manual && p.state !== "checking" && p.state !== "queued" ? " Checked again: " + (p.state === "passed" ? "now passes; press Resume, then Start now (it checks once more)." : String(p.text || "") + ".") : "";
  return {continued:false, text:"The rig check before " + String(f.label || "the show") + " FAILED: " + bad.join("; ") + "." + re,
          confirm:"Hold to continue anyway: the show starts now, late" + (off.length ? ", with " + off.join(", ") + " left off for this show" : "") +
            ". Still failing: " + bad.join("; ") + ".",
          holdMs:Math.round(1000 * (typeof p.continue_hold_s === "number" ? p.continue_hold_s : 1))};
}
// The alert hold (alerthold.py): the state card's word and sentence while
// the schedule is held at showtime because an alert is open, or null.
function alertHoldLook(ah){
  if(!ah || ah.held !== true) return null;
  return {word:"Held", label:String(ah.text || "Held: alert open.")};
}
// The booth line's note count (boothaudio.notes_view, 2026-10-28):
// ", 24 of 24 notes found", or which are missing (the first six by name).
function boothNotesText(n){
  if(!n || typeof n.expected !== "number" || typeof n.found !== "number") return "";
  const miss = Array.isArray(n.missing) ? n.missing.map(String) : [];
  let t = ", " + n.found + " of " + n.expected + " notes found";
  if(miss.length){
    t += "; missing " + miss.slice(0, 6).join(", ");
    if(miss.length > 6) t += " and " + (miss.length - 6) + " more";
  }
  return t;
}
// Operator announcements (announce.py, Jeff 2026-10-26): what the rack
// screen's Announcements block and its banner show, from the status's
// "announce". One press plays. A recording not on this PC yet says
// Recording missing and its button is off; so is every button while the
// announcement check (a.route: through the show audio, or the
// announcement's own output while no show audio runs) says it cannot
// play, while one is playing, or while the file could not be used. The
// schedule's state never turns them off (Jeff's field report, 2026-10-28:
// announcements and the schedule are independent). Stop is on only while
// one plays. The engine refuses anything this page lets through anyway.
function announceLook(an, booth){
  const a = an || {};
  if(!a.configured) return {show:false, buttons:[], banner:"", word:"", stopOff:true, device:"", devBad:false, note:""};
  const playing = a.playing || null;
  const bad = a.ok === false ? (a.error || "The announcements file could not be used.") : "";
  const rt = a.route || {};
  const devOk = !!rt.ok;
  const buttons = (a.items || []).map(i => {
    let sub = "", off = false;
    if(i.missing){ sub = "Recording missing"; off = true; }
    else if(!i.available){ sub = "Cannot play"; off = true; }
    else sub = (typeof i.length_s === "number") ? Math.round(i.length_s) + " s" : "";
    if(bad || !devOk || playing) off = true;
    return {id:String(i.id || ""), label:String(i.name || i.id || ""), sub:sub, off:off,
            missing:!!i.missing, title:String(i.reason || ""), on:!!(playing && playing.id === i.id)};
  });
  let banner = "", word = "";
  if(playing){
    // "Playing" only once audio has gone to the output (heard); until then
    // it is starting (Jeff's field report, 2026-10-28).
    const starting = playing.heard === false;
    word = playing.held_schedule ? "HELD FOR ANNOUNCEMENT" : starting ? "ANNOUNCEMENT STARTING" : "ANNOUNCEMENT PLAYING";
    const el = (typeof playing.elapsed_s === "number") ? Math.round(playing.elapsed_s) : null;
    const len = (typeof playing.length_s === "number") ? Math.round(playing.length_s) : null;
    banner = String(playing.name || "") + (starting ? " (waiting for the output to take it)" : el !== null && len !== null ? " (" + el + " s of " + len + " s)" : "") +
      (playing.held_schedule ? ". The schedule stays held until Resume." : ". " + String(playing.note || "It plays over the show; nothing was paused.")) +
      (playing.sent ? " Sent " + sentWords(playing.sent) + "." : "");
  } else if(a.held_failed){
    // The schedule was held for a press whose sound then failed: it stays
    // held until Resume, and this says why (review of PR #129, 4).
    word = "HELD, ANNOUNCEMENT FAILED";
    banner = String(a.held_failed.name || "The announcement") + " did not play: " +
      String(a.held_failed.why || "no reason given") + ". The schedule stays held until Resume.";
  }
  // Where a press plays, said apart from the Show audio lamp (Jeff's
  // field report, 2026-10-28, item 10): the lamp is the show's own
  // interface, this line is the announcements' route.
  let device;
  if(rt.kind === "show") device = "Plays on: " + String(rt.text || "the show audio") + (devOk ? "" : " (not open)");
  else if(rt.kind === "device" && a.device_set) device = "Plays on: " + String(rt.text || a.device || "") + (devOk ? "" : " (cannot use it)");
  else device = "Plays on: the show audio while it runs; no output of its own is picked";
  const b = booth || {};
  device += b.configured ? ". Booth speaker: " + String(b.device || "") : ". Booth speaker: not set";
  device += boothNotesText(b.notes);
  let note = bad || (!devOk ? String(rt.why || a.device_reason || "") : "");
  if(!note && !playing) note = a.over_show ? "A show is running: an announcement plays over it and pauses nothing. Stop ends the show." :
    "Between shows: an announcement holds the schedule first. Only Resume carries on. With no show to hold it just plays.";
  // A stale engine word (announce_view's "stale"): every play button off,
  // but Stop stays pressable, in case one is still playing.
  return {show:true, buttons:buttons, banner:banner, word:word, stopOff:!playing && !a.stale, device:device,
          devBad:!devOk, note:note};
}
// What an announcement actually sent, in plain words (remote.py
// sent_words says the same): route, output, audio system, channels,
// seconds, peak level.
function sentWords(x){
  if(!x || typeof x !== "object") return "";
  let w = (x.route === "show audio" ? "through the show audio on " : "on its own output ") + String(x.device || "?");
  if(x.hostapi) w += " (" + String(x.hostapi) + ")";
  const pk = (typeof x.peak_dbfs === "number") ? x.peak_dbfs.toFixed(1) + " dBFS" : "silence";
  return w + ", " + x.channels + " channel(s), " + Number(x.length_s || 0).toFixed(1) + " s, peak " + pk;
}
// The announcement output picker (Programming, Announcement output): the
// options for the outputs the engine lists. An output whose name is on
// more than one output is told apart by its audio system. Pure.
function annOutputOptions(list, current){
  const out = [{value:JSON.stringify({device:"", hostapi:null}), label:"None: only through the show audio", picked:false}];
  (list || []).forEach(o => out.push({
    value: JSON.stringify({device:String(o.name || ""), hostapi:o.same_name ? String(o.hostapi || "") : null}),
    label: String(o.label || o.name || "") + (o.picked ? ", picked now" : ""),
    picked: !!o.picked, disabled: !!o.cannot_pick}));
  if(!out.some(o => o.picked)){
    // A name in the file that matches none, or more than one, output:
    // said at the top until one is chosen; saving it changes nothing.
    if(current) out.unshift({value:"", label:String(current) + ": choose which output", picked:true});
    else out[0].picked = true;
  }
  return out;
}
// The Fireball page (fireball.py). The body of one heartbeat: every
// control's state, only true for one that is on.
function fireballBody(on, run, seq, change){
  const c = {};
  for(const k of ["fuel", "air", "valve", "igniter", "purge"]) c[k] = on[k] === true;
  return {controls:c, run:run === undefined ? null : run, seq:seq, change:!!change};
}
// What the page may do now, and what it says: v is GET fireball, on the
// page's own channel state, stale and viewOnly the page's.
// {ok: a press may start, cut: everything on must go off now, armed,
// text: the line under the heading, block: why nothing may be pressed}.
function fireballState(v, on, stale, viewOnly){
  const any = Object.keys(on || {}).some(k => on[k]);
  const toggles = ["fuel", "air", "purge"].filter(k => (on || {})[k]);
  const out = {ok:false, cut:"", armed:false, text:"", block:""};
  if(!v){ out.block = "Not known yet."; return out; }
  if(!v.available){ out.block = v.problem || "There is no fireball group."; out.cut = any ? out.block : ""; return out; }
  out.armed = v.armed === "armed";
  if(stale) out.block = "This page's status is not live, so nothing can be pressed.";
  else if(viewOnly) out.block = "Signed out: this screen is view only. Sign in to use the Fireball controls.";
  else if(v.blocked) out.block = "Not available now: " + v.blocked;
  else if(v.owner && !v.mine) out.block = v.owner + " is using the Fireball controls. One screen at a time.";
  if(out.block){ out.cut = any ? out.block : ""; }
  if(!out.armed){
    out.text = toggles.length ? "Turn " + toggles.map(k => ({fuel:"Fuel", air:"Air", purge:"Purge"})[k]).join(", ") + " off before arming." :
      (v.armed_why || "The Fireball is not armed.") + " Every channel stays at zero until it is: nothing on this page arms.";
    if(any && !out.cut) out.cut = "the Fireball is not armed";
    if(!out.block) out.block = "Arm the Fireball first.";
  } else {
    out.text = "Armed. Fuel Charge, Air Charge and Purge stay on until pressed again; Valve Open and Igniter are on only while held. Stop and any disarm turn everything off; a Hold does not.";
  }
  out.ok = !out.block;
  return out;
}
// Wave flamer cleaning (cleaning.py). One beat of a hold on the button:
// "hold" is true only while the finger is down.
function cleaningBody(hold, run, seq, change){
  return {hold:hold === true, run:run === undefined ? null : run, seq:seq, change:!!change};
}
// What the page may do now: v is GET cleaning (or a beat's reply),
// holding the page's own hold, stale and viewOnly the page's.
// {ok: a hold may start, cut: a hold under way must end now, block,
// word: the button's own words, na: the button is not for pressing now}.
function cleaningState(v, holding, stale, viewOnly){
  const out = {ok:false, cut:"", block:"", word:"Hold to start the cleaning", na:true};
  const st = v && v.state || "idle";
  if(!v) out.block = "Not known yet.";
  else if(!v.available) out.block = v.problem || "There is no wave flamer group.";
  else if(stale) out.block = "This page's status is not live, so a hold cannot start.";
  else if(viewOnly) out.block = "Signed out: this screen is view only. Sign in to start the cleaning.";
  else if((st === "starting" || st === "acking") && !v.mine) out.block = (v.holder || "Another screen") + " is holding the button. One screen at a time.";
  else if((st === "idle" || st === "paused") && v.blocked) out.block = "Not available now: " + v.blocked;
  if(st === "phase1"){ out.word = "Phase 1 runs on its own: you can let go"; if(!out.block) out.block = "Phase 1 is running on its own. Press Stop to end it."; }
  else if(st === "phase2"){ out.word = "Firing the leftover fuel"; if(!out.block) out.block = "The purge bursts are running. Press Stop to end them."; }
  else if(st === "paused" || st === "acking") out.word = holding ? "Keep holding..." : "Hold to start the purge bursts";
  else out.word = holding ? "Keep holding..." : "Hold to start the cleaning";
  out.cut = holding && out.block && st !== "phase1" && st !== "phase2" ? out.block : "";
  out.ok = !out.block;
  out.na = !out.ok && !holding;
  return out;
}
// Where a run is, in plain words, from the engine's progress.
function cleaningWords(p){
  if(!p) return "";
  const parts = [(p.phase === 1 ? "Phase 1 of 2: " : "Phase 2 of 2: ") + p.text +
    (p.left_s !== null && p.left_s !== undefined ? " (" + Number(p.left_s).toFixed(1) + " s left)" : "") + "."];
  if(p.next) parts.push("Next: " + p.next + ".");
  if(p.phase === 2) parts.push("Bursts: " + (p.bursts || 0) + ".");
  parts.push("Running " + Math.floor(p.elapsed_s || 0) + " s.");
  return parts.join(" ");
}
// A hazer's tile or button (hazers.py, Jeff 2026-10-29): ARMED or
// DISARMED in words, what a tap does, and a line with its state and levels.
// A plain tap, never a hold: a hazer is not a flame. The state is the
// engine's, from flamesafe's own word ("waiting" until flamesafe has taken
// the arm). Stale reads as not known; a tap that disarms is never held back.
function hazerLook(h, stale){
  h = h || {};
  const armed = !!h.armed, name = h.name || "Hazer";
  const word = armed ? "ARMED" : "DISARMED";
  const out = {armed: armed, word: word, state: word, act: armed ? "Tap to disarm" : "Tap to arm",
    cls: armed ? "armed" : "disarmed", label: name + " " + word, line: ""};
  const lv = "Haze " + (h.haze_pct || 0) + "%, fan " + (h.fan_pct || 0) + "%";
  if(stale){ out.cls = "unknown"; out.line = "Not known: no fresh status"; return out; }
  const st = {running: "Running", held: "Paused by Hold", standby: "Runs when the show plays",
    waiting: "Waiting for flamesafe"}[h.state] || "";
  out.line = armed && st ? st + ". " + lv : lv;
  if(armed && h.state === "waiting") out.cls = "held";
  if(armed && h.state === "held"){ out.state = "ARMED, paused by Hold"; out.label = name + " ARMED, paused by Hold"; }
  return out;
}
// One step of a level stepper: 0 to 100 percent, never outside.
function hazerStep(pct, delta){
  const v = Math.round(Number(pct) || 0) + Number(delta || 0);
  return Math.max(0, Math.min(100, v));
}
// The timed purge (Jeff, 2026-10-29; morning.py). What the Startup page's
// control may do now: mo is GET morning, secs the seconds chosen here,
// holding this page's own start hold, stale and viewOnly the page's.
// {ok: a hold may start, block: why not, word: the button's words (the
// machines and the seconds, the confirmation), cut: a hold under way must
// end now, running}. The engine times the hold and decides; this only
// says what the button may do.
function timedPurgeNames(mo){
  const sel = ((mo && mo.purge_state) || {}).selected || [];
  return ((mo && mo.machines) || []).filter(m => sel.includes(m.id)).map(m => m.name);
}
function timedLeft(s){ return Math.ceil(Math.max(0, Number(s) || 0)) + " s"; }
// The custom box: a whole number, kept within what this show machine
// allows (the engine refuses anything else anyway). null for no number.
function timedClamp(v, min, max){
  const n = Math.round(Number(v));
  if(String(v).trim() === "" || !isFinite(n)) return null;
  return Math.min(max, Math.max(min, n));
}
function timedPurgeLook(mo, secs, holding, stale, viewOnly){
  const out = {ok:false, block:"", word:"Hold to start a timed purge", cut:"", running:false};
  const t = (mo && mo.timed) || {}, ts = (mo && mo.timed_settings) || {};
  const min = Number(ts.min_s) || 1, max = Number(ts.max_s) || 30;
  const names = timedPurgeNames(mo);
  if(t.running){
    out.running = true;
    out.word = "PURGING " + (t.machines || []).join(", ") + ": " + timedLeft(t.left_s) + " left";
    out.block = "A timed purge is running. Stop purge ends it now, and so does any other purge press.";
    return out;
  }
  if(!mo) out.block = "Not known yet.";
  else if(ts.error) out.block = "No timed purge: " + ts.error;
  else if(stale) out.block = "This page's status is not live, so a hold cannot start.";
  else if(viewOnly) out.block = "Signed out: this screen is view only. Sign in to purge.";
  else if(!mo.purge_enabled) out.block = "Purge arming is switched off for this device.";
  else if(mo.live_refusal) out.block = "Purge not allowed now: " + mo.live_refusal;
  else if(!names.length) out.block = "Click a machine or a group first.";
  else if(!(Number.isInteger(secs) && secs >= min && secs <= max)) out.block = "Choose " + min + " to " + max + " seconds.";
  else if(t.holding && !t.mine) out.block = (t.holder || "Another screen") + " is holding to start a timed purge. One screen at a time.";
  else if((mo.purge_state || {}).firing) out.block = "A purge is firing. One purge at a time.";
  if(names.length && Number.isInteger(secs) && (holding || !out.block))
    out.word = (holding ? "Keep holding: " : "Hold to purge ") + names.join(", ") + " for " + secs + " s";
  out.cut = holding && out.block ? out.block : "";
  out.ok = !out.block;
  return out;
}
// The banner on every screen while one runs: what, how long, who.
function timedBanner(t){
  if(!t) return "";
  return (t.machines || []).join(", ") + ": " + timedLeft(t.left_s) + " left of " + t.seconds + " s. Started on the " +
    (t.device || "rack screen") + (t.who ? " by " + t.who : "") + ".";
}
// LOGIC END
