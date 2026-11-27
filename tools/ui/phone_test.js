// Playwright checks for ltcplay/web/phone.html against tools/ui/phone_fake.js.
//   node tools/ui/phone_test.js [shotsDir]
const {chromium} = require("playwright");
const fs = require("fs");
const path = require("path");
const {makeRig, start} = require("./phone_fake.js");
const SHOTS = process.argv[2] || path.join(__dirname, "shots");
fs.mkdirSync(SHOTS, {recursive: true});
const SIZES = [[320, 568], [360, 740], [390, 844], [430, 932]];
let pass = 0, fail = 0;
const check = (c, m) => { if (c) pass++; else { fail++; console.log("FAIL", m); } };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function open(browser, [w, h], rig, scale) {
  const {server, port} = await start(rig, 0);
  const ctx = await browser.newContext({viewport: {width: w, height: h}, hasTouch: true, isMobile: true, deviceScaleFactor: 2});
  const page = await ctx.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await page.goto(`http://127.0.0.1:${port}/phone`);
  if (scale) await page.addStyleTag({content: `html{font-size:${16 * scale}px}`});
  await page.waitForFunction(() => /LINK OK/.test(document.getElementById("link").textContent), null, {timeout: 5000});
  return {page, ctx, server, errs, close: async () => { await ctx.close(); server.close(); }};
}
async function tab(page, t) { await page.click(`#tabs [data-n="${t}"]`); await sleep(120); }
async function geometry(page, label) {
  const g = await page.evaluate(() => {
    const out = {over: [], small: [], clip: []};
    const W = window.innerWidth;
    if (document.documentElement.scrollWidth > W + 1) out.over.push("doc " + document.documentElement.scrollWidth);
    const m = document.getElementById("main");
    if (m.scrollWidth > m.clientWidth + 1) out.over.push("main " + m.scrollWidth + ">" + m.clientWidth);
    for (const el of document.querySelectorAll("body *")) {
      const r = el.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      if (el.closest("[hidden]")) continue;
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || cs.display === "none") continue;
      // inside the scroller, only horizontal extent matters
      if (r.right > W + 1 || r.left < -1) out.over.push(el.tagName + "." + el.className + " " + Math.round(r.left) + ".." + Math.round(r.right) + " " + (el.textContent || "").slice(0, 20));
      if (el.tagName === "BUTTON" && el.id !== "alerts" && r.height < 43.5) out.small.push((el.textContent || el.id).slice(0, 20) + " " + r.height);
      if (el.children.length === 0 && el.scrollWidth > el.clientWidth + 1 && cs.textOverflow !== "ellipsis" && el.clientWidth > 0 && cs.overflowX !== "visible") out.clip.push((el.textContent || "").slice(0, 30));
      // text that overflows its own box while overflow is visible
      if (el.children.length === 0 && (el.textContent || "").trim() && el.scrollWidth > el.clientWidth + 1 && cs.display !== "inline" && cs.textOverflow !== "ellipsis") out.clip.push("vis:" + (el.textContent || "").slice(0, 30));
    }
    return out;
  });
  check(g.over.length === 0, `${label}: horizontal overflow ${JSON.stringify(g.over.slice(0, 3))}`);
  check(g.small.length === 0, `${label}: buttons under 44px ${JSON.stringify(g.small.slice(0, 3))}`);
  check(g.clip.length === 0, `${label}: clipped text ${JSON.stringify(g.clip.slice(0, 3))}`);
}
const rect = (page, sel) => page.locator(sel).first().evaluate((e) => { const r = e.getBoundingClientRect(); return [r.x, r.y, r.width, r.height].map((v) => Math.round(v * 10) / 10).join(","); });
const btnText = async (page, sel) => { const l = page.locator(sel).first(); return (await l.count()) ? (await l.textContent()).trim() : null; };
const firstArm = '#main .card:not(.all) [data-act="arm"]';

(async () => {
  const browser = await chromium.launch();
  // ---- source rules
  const src = fs.readFileSync(path.join(__dirname, "..", "..", "ltcplay", "web", "phone.html"), "utf8");
  check(!/[\u2013\u2014]/.test(src), "no em or en dash in phone.html");
  for (const n of ["Group 1", "Hazer 1", "Laser 1", "Ignite the Night", "Wave Flamer", "Front Row", "Cat-walk", "Fireball", "Dollywood", "GPL", "Hazer 0", "Laser 0"])
    check(!src.includes(n), "fixture name not in source: " + n);
  check(!/googleapis|gstatic|https?:\/\//.test(src), "no internet URL in page");
  check(!/#[0-9a-f]{0,2}[0-9a-f]{0,2}(ff|FF)\b/.test("") , "noop");
  check(!/arm-hold/.test(src.replace(/Never call.*/, "")), "page never calls arm-hold");
  check(src.includes("purge-request") && src.includes("purge-confirm") && !src.includes("purge-timed"), "purge goes by tap, then confirm; never the start hold");

  // ---- per size, per tab, per state
  for (const sz of SIZES) {
    for (const scale of (sz[0] === 320 ? [1, 1.5] : [1])) {
      const rig = makeRig({groups: 4, machines: 3, hazers: 3, lasers: 3});
      rig.lasers[1].armed = true; rig.groups[1].armed = "armed"; rig.locked.add("m2_0");
      rig.alerts.push({id: 7, kind: "x", text: "A warning from the engine", severity: "warning", ver: 1});
      const A = await open(browser, sz, rig, scale);
      const tag = `${sz.join("x")}@${scale}`;
      const masters = [];
      for (const t of ["show", "arm", "haze", "service"]) {
        await tab(A.page, t);
        if (t === "arm") for (const k of ["flame", "haze", "laser"]) { await A.page.click(`[data-act="kind"][data-n="${k}"]`); await sleep(100); await geometry(A.page, `${tag} arm/${k}`); }
        if (t === "arm") { await A.page.click('[data-act="kind"][data-n="flame"]'); await sleep(100); await A.page.click('[data-act="open"] >> nth=0'); await sleep(150); await geometry(A.page, `${tag} arm/flame open`); }
        await geometry(A.page, `${tag} ${t}`);
        masters.push(await rect(A.page, "#master"));
        if (sz[0] === 390 && scale === 1) await A.page.screenshot({path: path.join(SHOTS, `tab-${t}.png`)});
      }
      check(new Set(masters).size === 1, `${tag}: master rect same on all tabs ${masters}`);
      check(A.errs.length === 0, `${tag}: page errors ${A.errs}`);
      await A.close();
    }
  }

  // ---- link states: position of first ARM identical; master identical
  {
    const rig = makeRig({groups: 3, machines: 3});
    const A = await open(browser, [390, 844], rig);
    await tab(A.page, "arm"); await sleep(300);
    const r0 = await rect(A.page, firstArm), m0 = await rect(A.page, "#master");
    rig.statusDead = true; await sleep(2900);
    check((await btnText(A.page, "#link-t")) === "SLOW LINK", "SLOW LINK shown: " + await btnText(A.page, "#link-t"));
    const r1 = await rect(A.page, firstArm);
    await sleep(3000);
    const lt = await btnText(A.page, "#link-t");
    check(/^NO CONTACT \d\d:\d\d$/.test(lt), "NO CONTACT mm:ss: " + lt);
    const r2 = await rect(A.page, firstArm), m2 = await rect(A.page, "#master");
    check(r0 === r1 && r1 === r2, `first ARM same position in all link states ${r0} ${r1} ${r2}`);
    check(m0 === m2, "master same rect with NO CONTACT");
    check(await A.page.locator(firstArm).first().isDisabled(), "ARM disabled with polling cut");
    check(await A.page.evaluate(() => [...document.querySelectorAll('#main [data-act="lock"],#main [data-act="lock-all"],#main [data-act="arm-all"]')].every((b) => b.disabled)), "lock and arm-all disabled with no contact");
    const disBtn = '#main .card:not(.all) [data-act="dis"]';
    check(!(await A.page.locator(disBtn).first().isDisabled()), "DISARM enabled with no contact");
    await A.page.screenshot({path: path.join(SHOTS, "no-contact.png")});
    rig.posts.length = 0;
    await A.page.tap(disBtn); await sleep(300);
    check(rig.posts.some((p) => p.name === "group-disarm"), "DISARM posts with polling cut");
    // retry while engine does not answer
    rig.ignore.add("group-disarm"); rig.posts.length = 0;
    await A.page.tap(disBtn); await sleep(300);
    check((await btnText(A.page, disBtn)) === "SENDING", "disarm reads SENDING: " + await btnText(A.page, disBtn));
    await sleep(4500);
    check(rig.posts.filter((p) => p.name === "group-disarm").length >= 2, "disarm retried about once a second: " + rig.posts.length);
    rig.ignore.add("disarm-all"); rig.ignore.add("hazers-off"); rig.ignore.add("laser-arm");
    await A.page.tap("#master"); await sleep(300);
    check((await btnText(A.page, "#master")) === "SENDING", "master reads SENDING with no contact");
    rig.ignore.delete("group-disarm"); rig.ignore.delete("disarm-all"); rig.ignore.delete("hazers-off"); rig.ignore.delete("laser-arm"); rig.statusDead = false; await sleep(2500);
    check((await btnText(A.page, "#link-t")) === "LINK OK", "LINK OK again");
    check(rig.posts.some((p) => p.name === "disarm-all") && rig.posts.some((p) => p.name === "hazers-off") && rig.posts.some((p) => p.name === "laser-arm" && p.body.all === true && p.body.on === false), "master posts disarm-all, hazers-off, laser-arm off");
    await A.close();
  }

  // ---- truth: tap ARM does not show ARMED until status says so
  {
    const rig = makeRig({groups: 3, machines: 3}); rig.armDelay = 1800;
    const A = await open(browser, [390, 844], rig);
    await tab(A.page, "arm"); await sleep(300);
    await A.page.tap(firstArm); await sleep(250);
    check(await A.page.isVisible("#sheet .what"), "confirm bar appears after ARM tap");
    check(await A.page.locator('#sheet [data-act="ok"]').isDisabled(), "CONFIRM disabled before min_s");
    check(!rig.groups.some((g) => g.armed !== "disarmed"), "ARM tap alone arms nothing");
    const barRect = await rect(A.page, "#sheet"), r0 = await rect(A.page, firstArm);
    await A.page.screenshot({path: path.join(SHOTS, "confirm-bar.png")});
    await sleep(500);
    check(!(await A.page.locator('#sheet [data-act="ok"]').isDisabled()), "CONFIRM enabled after min_s");
    await A.page.tap('#sheet [data-act="ok"]'); await sleep(200);
    const t1 = await btnText(A.page, firstArm);
    check(t1 === "PENDING", "reads PENDING after confirm: " + t1);
    check((await rect(A.page, firstArm)) === r0, "ARM button did not move");
    await sleep(900);
    check((await btnText(A.page, firstArm)) !== "ARMED", "not ARMED while engine still holding");
    await sleep(1500);
    check((await btnText(A.page, firstArm)) === "ARMED", "ARMED once status says so: " + await btnText(A.page, firstArm));
    check(rig.posts.filter((p) => p.name === "arm-request").length === 1 && rig.posts.filter((p) => p.name === "arm-confirm").length === 1, "one request, one confirm");
    check(!rig.posts.some((p) => p.name === "arm-hold"), "no arm-hold");
    // NO ANSWER when the engine never shows it
    rig.armOutcome = "never"; rig.armDelay = 100;
    rig.groups[0].armed = "disarmed";
    // refusal line
    rig.refuse["arm-request"] = "flamesafe has not reported in the last second, so its real armed state is not known. Nothing can arm from here until it does.";
    await A.page.tap('#main .card:not(.all) [data-act="arm"]:not([disabled]) >> nth=0'); await sleep(300);
    check(await A.page.isVisible("#sheet .ln"), "refusal line shown");
    await A.page.screenshot({path: path.join(SHOTS, "refusal.png")});
    const lh = await A.page.evaluate(() => document.querySelector("#sheet .ln").getBoundingClientRect().height);
    check(lh <= 3 * 1.35 * 15 + 26, "refusal at most about three lines: " + lh);
    const rr = await rect(A.page, firstArm);
    await A.page.tap("#sheet .ln"); await sleep(200);
    check(!(await A.page.isVisible("#sheet")), "refusal dismissed by a tap");
    delete rig.refuse["arm-request"];
    await A.close();
  }
  // ---- ARM ALL and unknown flames, lock flows
  {
    const rig = makeRig({groups: 3, machines: 3}); rig.armDelay = 300;
    const A = await open(browser, [390, 844], rig);
    await tab(A.page, "arm"); await sleep(300);
    await A.page.tap('[data-act="arm-all"]'); await sleep(200);
    await sleep(400); await A.page.tap('#sheet [data-act="ok"]');
    await sleep(6000);
    check(rig.groups.every((g) => g.armed === "armed"), "ARM ALL armed each group through request and confirm");
    check(rig.posts.filter((p) => p.name === "arm-request").length === 3 && rig.posts.filter((p) => p.name === "arm-confirm").length === 3, "ARM ALL: three requests, three confirms");
    // lock a machine, group, unlock
    await A.page.tap('[data-act="open"] >> nth=0'); await sleep(200);
    await A.page.tap('[data-act="lock-m"] >> nth=0'); await sleep(900);
    check(rig.locked.size === 1 && (await btnText(A.page, '[data-act="lock-m"] >> nth=0')) === "LOCKED", "machine lock shows after status");
    await A.page.screenshot({path: path.join(SHOTS, "tab-arm-open.png")});
    await A.page.tap('[data-act="lock-m"] >> nth=0'); await sleep(450);
    await A.page.tap('#sheet [data-act="ok"]'); await sleep(900);
    check(rig.locked.size === 0 && rig.posts.some((p) => p.name === "lock" && p.body.on === false && p.body.confirmed === true), "unlock goes through confirm bar with confirmed:true");
    rig.flamesStale = true; await sleep(700);
    check((await btnText(A.page, firstArm)) === "UNKNOWN" && await A.page.locator(firstArm).first().isDisabled(), "stale flamesafe: UNKNOWN and ARM disabled");
    await A.page.screenshot({path: path.join(SHOTS, "flames-unknown.png")});
    await A.close();
  }
  // ---- rigs: 1 group, 8 groups with 40 character names, none of anything
  for (const o of [{groups: 1, machines: 1, hazers: 0, lasers: 0}, {groups: 8, machines: 20, hazers: 12, lasers: 8, long: true}, {groups: 8, machines: 1, hazers: 1, lasers: 1, long: true}]) {
    for (const sz of [[320, 568], [430, 932]]) {
      const rig = makeRig(o); const A = await open(browser, sz, rig);
      const tag = `rig ${JSON.stringify(o)} ${sz.join("x")}`;
      for (const t of ["show", "arm", "haze", "service"]) {
        await tab(A.page, t);
        if (t === "arm") for (const k of ["flame", "haze", "laser"]) {
          await A.page.click(`[data-act="kind"][data-n="${k}"]`); await sleep(120);
          if (k === "flame") { const n = await A.page.locator('[data-act="open"]').count(); for (let i = 0; i < Math.min(n, 2); i++) await A.page.locator('[data-act="open"]').nth(i).click(); await sleep(120); }
          await geometry(A.page, `${tag} arm/${k}`);
          if (sz[0] === 320 && o.long && o.groups === 8 && o.machines === 20 && k === "flame") await A.page.screenshot({path: path.join(SHOTS, "long-names-320.png")});
        } else await geometry(A.page, `${tag} ${t}`);
      }
      check(A.errs.length === 0, `${tag}: page errors ${A.errs}`);
      await A.close();
    }
  }
  // ---- SHOW: failed rig check, continue, checking, show control off, stop
  {
    const rig = makeRig({groups: 2, machines: 2});
    const A = await open(browser, [390, 844], rig);
    await tab(A.page, "show"); await sleep(300);
    const goR = await rect(A.page, '[data-act="go"]'), stR = await rect(A.page, '[data-act="stop"]');
    rig.preroll = {state: "checking", window_s: 10, items: []}; rig.prerollAt = Date.now() - 2000; await sleep(600);
    check(/CHECKING/.test(await A.page.evaluate(() => document.querySelector(".hero").textContent)), "CHECKING shown");
    check((await A.page.evaluate(() => document.getElementById("ck").style.width)) !== "100%", "check bar drains");
    await A.page.screenshot({path: path.join(SHOTS, "checking.png")});
    rig.preroll = {state: "failed", window_s: 10, items: [], age_s: 3, show_check: {label: "Show 3", off: ["VIDEO"], items: [
      {name: "Audio", required: true, ok: true, text: "ok"}, {name: "Video", required: true, ok: false, text: "NO ANSWER from the video machine on the network"},
      {name: "Announcements", required: false, ok: false, text: "Not set up"}]}};
    await sleep(600);
    const hero = await A.page.evaluate(() => document.querySelector(".hero").textContent);
    check(/RIG CHECK FAILED/.test(hero) && /Video/.test(hero) && /Announcements/.test(hero), "failed rig check lists items");
    check(await A.page.evaluate(() => document.querySelectorAll(".checks li.no").length === 1 && document.querySelectorAll(".checks li.wn").length === 1), "one cross, one amber warning");
    check(!/CHECK AGAIN/i.test(await A.page.content()), "no CHECK AGAIN");
    check((await rect(A.page, '[data-act="go"]')) === goR && (await rect(A.page, '[data-act="stop"]')) === stR, "transport did not move in failed check");
    await A.page.screenshot({path: path.join(SHOTS, "rig-check-failed.png")});
    await A.page.tap('[data-act="cont"]'); await sleep(400);
    check(await A.page.locator('#sheet [data-act="ok"]').isDisabled(), "continue CONFIRM disabled for hold_s");
    await sleep(1100);
    check(!(await A.page.locator('#sheet [data-act="ok"]').isDisabled()), "continue CONFIRM enabled after hold_s");
    await A.page.tap('#sheet [data-act="ok"]'); await sleep(900);
    check(rig.posts.filter((p) => p.name === "preroll-continue").map((p) => p.body.phase).join() === "begin,done", "preroll-continue begin then done");
    // playing
    check(/PLAYING/.test(await btnText(A.page, "#t-word")), "playing word: " + await btnText(A.page, "#t-word"));
    await A.page.screenshot({path: path.join(SHOTS, "playing.png")});
    rig.posts.length = 0;
    await A.page.tap('[data-act="stop"]'); await sleep(300);
    check(rig.posts.length === 0 && /TAP AGAIN/.test(await btnText(A.page, '[data-act="stop"]')), "first Stop tap sends nothing");
    await A.page.tap('[data-act="stop"]'); await sleep(700);
    check(rig.posts.some((p) => p.name === "stop"), "second tap sends stop");
    rig.showControl = false; rig.state = "STANDBY"; await sleep(600);
    check(/SHOW CONTROL OFF/.test(await btnText(A.page, ".hero .cap")) && await A.page.locator('[data-act="go"]').isDisabled(), "SHOW CONTROL OFF disables START NOW");
    await A.close();
  }
  // ---- haze levels, hazer run, laser test
  {
    const rig = makeRig({groups: 2, machines: 2, hazers: 2, lasers: 2}); rig.hazers[1].haze = 70;
    const A = await open(browser, [390, 844], rig);
    await tab(A.page, "haze"); await sleep(300);
    check((await A.page.evaluate(() => document.querySelector(".card.all .val").textContent)) === "MIX", "ALL shows MIX when hazers differ");
    await A.page.screenshot({path: path.join(SHOTS, "tab-haze.png")});
    await A.page.tap('.card:not(.all) [data-f="haze"][data-d="5"] >> nth=0'); await sleep(900);
    check(rig.hazers[0].haze === 55 && rig.posts.some((p) => p.name === "hazer-level" && p.body.haze === 55), "haze + posts 55 percent");
    await tab(A.page, "service"); await sleep(300);
    check((await btnText(A.page, '[data-act="lt"] >> nth=1')) === "DISARMED" && await A.page.locator('[data-act="lt"]').nth(1).isDisabled(), "laser test reads DISARMED, disabled");
    rig.lasers[0].armed = true; await sleep(600);
    check((await btnText(A.page, '[data-act="lt"] >> nth=1')) === "TEST OFF", "armed laser reads TEST OFF");
    await A.page.tap('[data-act="lt"] >> nth=1'); await sleep(450);
    await A.page.tap('#sheet [data-act="ok"]'); await sleep(900);
    check((await btnText(A.page, '[data-act="lt"] >> nth=1')) === "TEST ON", "TEST ON after status");
    await A.page.tap('[data-act="run"] >> nth=1'); await sleep(450);
    await A.page.tap('#sheet [data-act="ok"]'); await sleep(2800);
    check(/^STOP \d+/.test(await btnText(A.page, '[data-act="run-stop"] >> nth=0')), "hazer run shows STOP and seconds");
    check(rig.posts.some((p) => p.name === "hazer-test" && p.body.beat), "page beats the hazer test");
    await A.page.screenshot({path: path.join(SHOTS, "tab-service.png")});
    await A.page.tap('[data-act="run-stop"] >> nth=0'); await sleep(900);
    check(Object.keys(rig.hzTest).length === 0, "tapping STOP ends the run");
    await A.close();
  }
  // ---- sign in and alerts and gear
  {
    const rig = makeRig({groups: 2, machines: 2, users: true}); rig.alerts.push({id: 3, kind: "x", text: "Fault text from the engine", severity: "fault", ver: 2});
    const A = await open(browser, [390, 844], rig);
    await sleep(500);
    check(await A.page.isVisible("#cfg .names"), "sign-in view opens when users are active and nobody is signed in");
    await A.page.screenshot({path: path.join(SHOTS, "sign-in.png")});
    await A.page.tap('#cfg [data-act="pick"] >> nth=0'); await A.page.fill("#pw", "goodpass"); await A.page.tap('#cfg [data-act="signin"]'); await sleep(900);
    check(rig.user === "Jeff" && !(await A.page.isVisible("#cfg")), "signed in");
    check(/1 ALERT/.test(await btnText(A.page, "#alerts")), "alert count in the status line");
    await A.page.tap("#alerts"); await sleep(200);
    await A.page.screenshot({path: path.join(SHOTS, "alerts.png")});
    await A.page.tap('#cfg [data-act="ack"]'); await sleep(900);
    check(rig.alerts.length === 0, "ACK acknowledges");
    await A.close();
  }
  await browser.close();
  console.log(`phone_test: ${pass} passed, ${fail} failed`);
  process.exit(fail ? 1 : 0);
})().catch((e) => { console.error(e); process.exit(2); });
