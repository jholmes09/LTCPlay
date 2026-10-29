// The audit's ten-task walkthrough (UI_AUDIT.md 7e) driven on interface
// 2.0 against the fake engine: taps, holds, typed entries, confirms,
// screens and the words that pop up, per task.
// Usage: node tools/ui/flows.js -> OUT/results/_flows2.json and
// OUT/screenshots/flow2_*.png (OUT as in audit.js).
const {chromium} = require("playwright");
const fs = require("fs"), path = require("path");
const {FakeEngine} = require("./fake_engine");
const OUT = process.env.OUT || path.join(require("os").tmpdir(), "ltc-ui-audit");
fs.mkdirSync(path.join(OUT, "screenshots"), {recursive: true});
fs.mkdirSync(path.join(OUT, "results"), {recursive: true});
const VP = {width: +(process.env.W || 1920), height: +(process.env.H || 1080)};

class Op {
  constructor(page, eng, name){ this.page = page; this.eng = eng; this.name = name; this.taps = 0; this.holds = 0; this.typed = 0;
    this.screens = new Set(["show"]); this.read = 0; this.log = []; this.modals = 0; this.notes = []; }
  async view(){ return this.page.evaluate(() => (location.hash || "#show").slice(1)); }
  async popupWords(){
    return this.page.evaluate(() => {
      const vis = el => el && el.getClientRects().length && !el.closest("[hidden]");
      let t = "";
      const sh = document.getElementById("sheet");
      if(vis(sh)) t += document.getElementById("sh-h").innerText + " " + document.getElementById("sh-body").innerText;
      const m = document.getElementById("msg-t");
      if(vis(m)) t += " " + m.innerText;
      return t.replace(/\s+/g, " ").trim();
    });
  }
  async after(what){
    await this.page.waitForTimeout(450);
    this.screens.add(await this.view());
    const pw = await this.popupWords();
    const wc = pw ? pw.split(/\s+/).length : 0;
    if(pw && pw !== this.lastPw){ this.read += wc; this.log.push(`${what} -> reads: "${pw.slice(0, 220)}" (${wc} words)`); }
    else this.log.push(what);
    this.lastPw = pw;
  }
  async tap(sel, what){ await this.page.click(sel, {timeout: 3000}); this.taps++; await this.after("tap " + (what || sel)); }
  async typeNoTap(sel, text, what){ await this.page.fill(sel, text); this.typed++; await this.after("type " + (what || sel)); }
  async hold(sel, ms, what){
    const b = await this.page.$(sel); const r = await b.boundingBox();
    await this.page.mouse.move(r.x + r.width / 2, r.y + r.height / 2); await this.page.mouse.down();
    await this.page.waitForTimeout(ms); await this.page.mouse.up(); this.holds++; await this.after(`hold ${what || sel} ${ms} ms`);
  }
  async yes(what){ if(await this.page.isVisible("#sheet")){ this.modals++; await this.tap('#sh-row [data-i="1"]', "confirm: " + (await this.page.innerText('#sh-row [data-i="1"]'))); } else this.notes.push("no confirm for " + what); }
  async shot(n){ await this.page.screenshot({path: path.join(OUT, "screenshots", "flow2_" + n + ".png")}); }
  result(){ return {task: this.name, taps: this.taps, holds: this.holds, typed: this.typed, confirms: this.modals, screens: [...this.screens], popup_words: this.read, steps: this.log, notes: this.notes}; }
}

const TASKS = [
  ["1 Sign in", "signed_out", async o => {
    await o.tap("#user", "user chip"); await o.tap('[data-name="Dallen"]', "name Dallen");
    await o.typeNoTap("#si-pw", "secret1", "password"); await o.shot("1_signin");
    await o.tap('#sh-row [data-i="1"]', "Sign in"); }],
  ["2 Start the night (Run)", "idle_operator", async o => {
    await o.shot("2_before"); await o.tap("#b-go", "Run show"); await o.yes("run"); await o.shot("2_after"); }],
  ["3 Startup checklist incl. purge", "idle_operator", async o => {
    await o.tap('.tab[data-tab="day"]', "Day"); await o.shot("3_day");
    o.notes.push("then 'Open in 1.0' on Startup and 1.0's own checklist and purge (unchanged in this PR)"); }],
  ["4 Arm everything before a show", "ready", async o => {
    await o.hold("#b-armall", 3000, "Hold: arm flames"); await o.tap("#lz-b", "Arm lasers"); await o.yes("arm lasers"); await o.shot("4_armed"); }],
  ["5 Pause and resume", "running", async o => {
    await o.tap("#b-hold", "Hold"); await o.shot("5_held"); await o.tap("#b-go", "Resume"); }],
  ["6 Read and clear an alert (2 alerts)", "alerts_open", async o => {
    await o.shot("6_alerts");
    for(let i = 0; i < 4; i++){
      const lbl = await o.page.$eval("#al-act", b => b.hidden ? "" : b.innerText).catch(() => "");
      if(!/Acknowledge/.test(lbl)) break;
      await o.tap("#al-act", "Acknowledge"); } }],
  ["7 Play an announcement and carry on", "ready", async o => {
    await o.tap('[data-ann="delayed"]', "Delayed"); await o.shot("7_announcing"); await o.tap("#b-go", "Resume"); }],
  ["8 Abort and get back to ready", "running", async o => {
    await o.tap("#b-abort", "Abort (1st tap)"); await o.tap("#b-abort", "Abort (2nd tap)"); await o.shot("8_aborted");
    await o.tap("#b-go", "Reset"); await o.hold("#b-armall", 3000, "Hold: arm flames");
    await o.tap("#lz-b", "Arm lasers"); await o.yes("arm lasers"); await o.shot("8_ready"); }],
  ["9 Stop and get ready for the next show", "running", async o => {
    await o.tap("#b-stop", "Stop show"); await o.shot("9_stop_confirm"); await o.yes("stop");
    await o.tap("#b-go", "Run show"); await o.yes("run"); await o.shot("9_ready"); }],
  ["10 End of night", "idle_operator", async o => {
    await o.tap('.tab[data-tab="day"]', "Day");
    o.notes.push("then 'Open in 1.0' on Startup and shutdown: 1.0's own Shutdown checklist, locks and sign out (unchanged in this PR)"); }],
  // A group's Disarm the Stream Deck does not act on (2026-10-29): the
  // engine forces the Abort's disarm and the page says so in amber.
  ["11 Disarm a group the deck does not answer", "deck_not_answering", async o => {
    await o.shot("11_armed"); await o.tap('[data-arm="0"]', "Disarm front row"); await o.shot("11_forced");
    const said = await o.page.$eval("#msg-t", e => e.textContent).catch(() => "");
    const cls = await o.page.$eval("#msg", e => e.className).catch(() => "");
    if(!/^Not confirmed: ALL groups disarm sent\./.test(said) || cls !== "warn") o.notes.push("FORCED DISARM NOT SAID: '" + said + "' " + cls); }],
];

(async () => {
  const b = await chromium.launch({args: ["--no-sandbox"]});
  const out = [];
  for(const [name, sc, fn] of TASKS){
    const ctx = await b.newContext({viewport: VP, hasTouch: true});
    const page = await ctx.newPage();
    const eng = new FakeEngine(sc); await eng.attach(page);
    await page.goto("http://rack.test/remote2?tour=0"); await page.waitForTimeout(1500);
    const o = new Op(page, eng, name);
    try{ await fn(o); }catch(e){ o.notes.push("STOPPED: " + e.message.split("\n")[0]); }
    const r = o.result(); r.posts = eng.posts.filter(p => !/status/.test(p));
    out.push(r);
    console.log(`${name}: ${r.taps} taps, ${r.holds} holds, ${r.typed} typed, ${r.confirms} confirms, screens ${r.screens.join("/")}, ${r.popup_words} words to read | posts ${r.posts.join(" ")} ${r.notes.length ? "| " + r.notes.join("; ") : ""}`);
    await ctx.close();
  }
  fs.writeFileSync(path.join(OUT, "results", "_flows2.json"), JSON.stringify(out, null, 1));
  await b.close();
})();
