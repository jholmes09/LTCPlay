// Interface 2.0's layout audit (UI_AUDIT.md's measuring script, pointed at
// ltcplay/web/remote2.html): every state and tab at seven windows against a
// fake engine (fake_engine.js; it never reaches a rig), flagging cut,
// clipped or covered text, overlapping controls, anything under Abort,
// targets under 44 px (56 on SHOW), text under 16 px (20 on SHOW), low
// contrast, and any scroll on SHOW.
// Usage (Playwright installed; NODE_PATH pointing at it if it is global):
//   node tools/ui/audit.js            results and screenshots in OUT
//   OUT=/some/folder VPS=1024x768 node tools/ui/audit.js
//   NOFONT=1 node tools/ui/audit.js   the fallback fonts, not Inter
// OUT defaults to a folder in the system temp folder, never the repo.
const {chromium} = require("playwright");
const fs = require("fs");
const path = require("path");
const {FakeEngine} = require("./fake_engine");

const OUT = process.env.OUT || path.join(require("os").tmpdir(), "ltc-ui-audit");
const SHOTS = path.join(OUT, "screenshots");
const RES = path.join(OUT, "results");
fs.mkdirSync(SHOTS, {recursive: true});
fs.mkdirSync(RES, {recursive: true});

const VIEWPORTS = process.env.VPS ? process.env.VPS.split(",").map(v => v.split("x").map(Number)) : [[1920, 1080], [1366, 768], [1280, 800], [1024, 768], [820, 1180], [768, 1024], [390, 844]];
const NO_SCROLL = ["1920x1080", "1366x768", "1280x800", "1024x768", "820x1180", "768x1024"];
// [scenario, view(hash), extra action]
const CASES = process.env.ONLY_CASES ? JSON.parse(process.env.ONLY_CASES) : [
  ["idle_operator", "show"], ["ready", "show"], ["signed_out", "show"], ["show_pending", "show"], ["running", "show"],
  ["held", "show"], ["aborted", "show"], ["alerts_open", "show"], ["alerts_open", "show", "alert_next"],
  ["armed_groups", "show"], ["locked", "show"], ["announcing", "show"], ["alert_hold", "show"],
  ["live_flame", "show"], ["getting_started", "show"], ["stale", "show"], ["admin", "show"],
  ["running", "show", "stop_confirm"], ["running", "show", "abort_first_tap"], ["running", "show", "help"], ["idle_operator", "show", "coach"],
  ["rig_checking", "show"], ["rig_failed", "show"], ["rig_continued", "show"], ["cleaning", "show"],
  ["all_live", "show"], ["hazers_on", "show"], ["hazers_held", "show"], ["hazers_on", "show", "hazer_sheet"], ["flames_unknown", "show"], ["rehearsal", "show"], ["cleaning", "day"], ["running", "schedule"],
  ["idle_operator", "day"], ["idle_operator", "schedule"], ["admin", "settings"], ["signed_out", "show", "signin"],
  ["ipad", "show"], ["long_name", "show"], ["long_name_out", "show"], ["no_logo", "show"], ["long_name", "day"],
];
const CASES_1 = [
  ["idle_operator", "show"], ["signed_out", "show"], ["show_pending", "show"], ["running", "show"],
  ["held", "show"], ["aborted", "show"], ["alerts_open", "show"], ["alerts_open", "show", "expand_alerts"],
  ["armed_groups", "show"], ["locked", "show"], ["announcing", "show"], ["alert_hold", "show"],
  ["live_flame", "show"], ["getting_started", "show"], ["stale", "show"], ["hazers_on", "show"],
  ["idle_operator", "tonight"], ["admin", "programming"], ["idle_operator", "programming"], ["programming", "programming"],
  ["idle_operator", "morning"], ["locked", "morning"], ["purge", "morning"],
  ["signed_out", "users"], ["admin", "users"], ["idle_operator", "fuel"], ["idle_operator", "fireball"],
  ["idle_operator", "show", "info"], ["running", "show", "stop_confirm"], ["running", "show", "abort_first_tap"],
];
const ONLY = process.env.ONLY_VP ? process.env.ONLY_VP.split(",") : null;

const MEASURE = () => {
  const out = {vw: innerWidth, vh: innerHeight, issues: [], texts: [], controls: []};
  const onShow0 = !!document.getElementById("t-show") && !document.getElementById("t-show").hidden;
  const CRIT = {"b-abort": 96};
  const onShow = !!document.getElementById("t-show") && !document.getElementById("t-show").hidden;
  function sel(el){
    if(!el || el === document.body) return "body";
    if(el.id) return "#" + el.id;
    const parts = [];
    let e = el;
    while(e && e !== document.body && parts.length < 4){
      let s = e.tagName.toLowerCase();
      if(e.id){ parts.unshift("#" + e.id); break; }
      const ds = Object.keys(e.dataset || {})[0];
      if(ds) s += `[data-${ds.replace(/[A-Z]/g, m => "-" + m.toLowerCase())}="${e.dataset[ds]}"]`;
      else if(e.classList.length) s += "." + [...e.classList].slice(0, 2).join(".");
      parts.unshift(s);
      e = e.parentElement;
    }
    return parts.join(" > ");
  }
  function shown(el){
    if(!el || !el.getClientRects().length) return false;
    const cs = getComputedStyle(el);
    if(cs.visibility === "hidden" || cs.display === "none" || Number(cs.opacity) === 0) return false;
    for(let p = el; p; p = p.parentElement){ if(p.hidden) return false; }
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }
  // What of an element can be seen: its box cut by the window and by every
  // box around it that clips (a scrolling pane).
  function visRect(el){
    const r = el.getBoundingClientRect();
    let L = Math.max(r.left, 0), T = Math.max(r.top, 0), R = Math.min(r.right, innerWidth), B = Math.min(r.bottom, innerHeight);
    for(let p = el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement){
      const cs = getComputedStyle(p);
      if(cs.overflowX !== "visible" || cs.overflowY !== "visible"){
        const q = p.getBoundingClientRect();
        L = Math.max(L, q.left); T = Math.max(T, q.top); R = Math.min(R, q.right); B = Math.min(B, q.bottom);
      }
    }
    return {left:L, top:T, right:R, bottom:B, width:Math.max(0, R - L), height:Math.max(0, B - T)};
  }
  function scrolls(el){
    for(let p = el.parentElement; p; p = p.parentElement){ const cs = getComputedStyle(p); if(cs.overflowY === "auto" || cs.overflowY === "scroll") return true; }
    return false;
  }
  function label(el){ return (el.innerText || el.value || el.getAttribute("aria-label") || el.title || "").replace(/\s+/g, " ").trim().slice(0, 90); }
  function issue(kind, el, detail, extra){ out.issues.push(Object.assign({kind, sel: sel(el), text: label(el).slice(0, 80), detail}, extra || {})); }
  const se = document.scrollingElement;
  // Page scroll
  out.scroll_h = se.scrollHeight; out.scroll_w = se.scrollWidth;
  if(se.scrollWidth > innerWidth + 1) issue("hscroll", document.body, `page is ${se.scrollWidth}px wide in a ${innerWidth}px window`);
  // The visible view
  const view = [...document.querySelectorAll("section.pane")].find(v => !v.hidden);
  out.view = view ? view.id : "";
  if(se.scrollHeight > innerHeight + 1){
    const below = [...document.querySelectorAll("button, input, select, [data-arm]")].filter(shown)
      .filter(e => e.getBoundingClientRect().top + scrollY >= innerHeight - 4)
      .map(e => sel(e) + (label(e) ? " '" + label(e).slice(0, 30) + "'" : ""));
    issue("vscroll", document.body, `page needs ${se.scrollHeight - innerHeight}px of vertical scroll; ${below.length} controls start below the fold`, {below: below.slice(0, 40), excess: se.scrollHeight - innerHeight});
  }
  const mainEl = document.querySelector("main");
  out.main_excess = mainEl ? mainEl.scrollHeight - mainEl.clientHeight : 0;
  if(onShow0 && out.main_excess > 1) issue("vscroll", mainEl, `the SHOW pane needs ${out.main_excess}px of scroll`, {excess: out.main_excess});
  const ab = document.getElementById("b-abort");
  const abR = ab && shown(ab) ? ab.getBoundingClientRect() : null;
  out.abort = abR ? {w: Math.round(abR.width), h: Math.round(abR.height), right: Math.round(innerWidth - abR.right), bottom: Math.round(innerHeight - abR.bottom),
    dim: document.getElementById("abortbar").classList.contains("idle"), opacity: getComputedStyle(document.getElementById("abortbar")).opacity,
    hit: (() => { const at = document.elementFromPoint(abR.left + abR.width / 2, abR.top + abR.height / 2); return !!(at && ab.contains(at)); })()} : null;
  // Interactive controls
  const modal = ["sheet", "coach"].map(id => document.getElementById(id)).find(m => m && !m.hidden);
  out.modal = modal ? modal.id : "";
  const ctrls = [...document.querySelectorAll("button, input, select, textarea, a[href], [role=button], [role=radio], [data-arm], .slot, .warnbox")].filter(shown).filter(e => !modal || modal.contains(e) || e.id === "b-abort");
  // Overlap is judged on what can be seen: a control scrolled out of its
  // pane is not over the footer.
  const rects = ctrls.map(e => [e, e.getBoundingClientRect()]);
  const vrects = ctrls.map(e => [e, visRect(e)]);
  for(const [e, r] of rects){
    const inShow = onShow && (document.getElementById("t-show").contains(e) || e.closest("footer"));
    const min = CRIT[e.id] || (inShow ? 56 : 44);
    const isWarn = e.classList.contains("warnbox");
    out.controls.push({sel: sel(e), text: label(e).slice(0, 60), w: Math.round(r.width), h: Math.round(r.height), disabled: !!e.disabled,
      top: Math.round(r.top + scrollY), left: Math.round(r.left)});
    if(!isWarn && (r.width < min || r.height < min) && e.tagName !== "INPUT" || (e.tagName === "INPUT" && e.type !== "checkbox" && r.height < 44) || (e.tagName === "INPUT" && e.type === "checkbox" && (r.width < 44 || r.height < 44)))
      issue(min > 56 ? "small_critical" : "small_target", e, `${Math.round(r.width)}x${Math.round(r.height)} px (min ${min})`);
    if(r.bottom > innerHeight + 1 && !scrolls(e)) issue("offscreen_y", e, `bottom ${Math.round(r.bottom)} in a ${innerHeight}px window that does not scroll`);
    if(r.left < -1 || r.right > innerWidth + 1) issue("offscreen_x", e, `spans x ${Math.round(r.left)}..${Math.round(r.right)} in a ${innerWidth}px window`);
  }
  // Overlap between interactive controls (not nested)
  for(let i = 0; i < vrects.length; i++) for(let j = i + 1; j < vrects.length; j++){
    const [a, ra] = vrects[i], [b, rb] = vrects[j];
    if(!ra.width || !ra.height || !rb.width || !rb.height) continue;
    if(a.contains(b) || b.contains(a)) continue;
    const ox = Math.min(ra.right, rb.right) - Math.max(ra.left, rb.left);
    const oy = Math.min(ra.bottom, rb.bottom) - Math.max(ra.top, rb.top);
    if(ox > 2 && oy > 2){
      const k = (a.id === "b-abort" || b.id === "b-abort") ? "abort_overlap" : "overlap";
      out.issues.push({kind: k, sel: sel(a) + "  X  " + sel(b), text: label(a).slice(0, 40) + " | " + label(b).slice(0, 40), detail: `${Math.round(ox)}x${Math.round(oy)} px overlap`});
    }
  }
  // Abort over any visible text block or card at scroll top and at scroll bottom
  if(abR){
    const blocks = [...document.querySelectorAll("main *, header *, footer *, #sheet *")].filter(e => shown(e) && e.children.length === 0 && (e.innerText || "").trim());
    for(const e of blocks){
      const r = visRect(e);
      if(r.width > 0 && r.height > 0 && Math.min(r.right, abR.right) - Math.max(r.left, abR.left) > 2 && Math.min(r.bottom, abR.bottom) - Math.max(r.top, abR.top) > 2)
        issue("abort_over_text", e, "the Abort sign sits over this text at the top of the page");
    }
  }
  // Covered controls: centre point hits something else
  for(const [e, r] of rects){
    if(e.classList.contains("warnbox")) continue;
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if(cy < 0 || cy > innerHeight || cx < 0 || cx > innerWidth) continue;
    const vr = visRect(e);
    if(cx < vr.left || cx > vr.right || cy < vr.top || cy > vr.bottom) continue;
    const at = document.elementFromPoint(cx, cy);
    if(at && !(e === at || e.contains(at) || at.contains(e))) issue("covered", e, "its centre is covered by " + sel(at));
  }
  // Text: clipping, ellipsis, small fonts, contrast, words
  function bg(el){
    for(let p = el; p; p = p.parentElement){
      const c = getComputedStyle(p).backgroundColor;
      const m = c.match(/rgba?\(([^)]+)\)/);
      if(m){ const v = m[1].split(",").map(Number); if(v.length < 4 || v[3] > 0.5) return v.slice(0, 3); }
      const bi = getComputedStyle(p).backgroundImage;
      if(bi && bi !== "none" && /gradient/.test(bi)){ const mm = bi.match(/rgba?\(([^)]+)\)/); if(mm) return mm[1].split(",").map(Number).slice(0, 3); }
    }
    return [0, 0, 0];
  }
  function lum(c){ const a = c.map(v => { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); }); return 0.2126 * a[0] + 0.7152 * a[1] + 0.0722 * a[2]; }
  function contrast(a, b){ const x = lum(a), y = lum(b); return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05); }
  const all = [...document.querySelectorAll("body *")].filter(e => !["SCRIPT", "STYLE", "OPTION"].includes(e.tagName));
  const seen = new Set();
  for(const e of all){
    if(!shown(e)) continue;
    const own = [...e.childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(" ").replace(/\s+/g, " ").trim();
    const cs = getComputedStyle(e);
    const r = e.getBoundingClientRect();
    // Clipping on any element with hidden overflow
    const clipX = e.scrollWidth > e.clientWidth + 1 && cs.overflowX !== "visible" && cs.overflowX !== "auto" && cs.overflowX !== "scroll";
    const clipY = e.scrollHeight > e.clientHeight + Math.max(6, e.clientHeight * 0.15) && cs.overflowY !== "visible" && cs.overflowY !== "auto" && cs.overflowY !== "scroll";
    const ell = cs.textOverflow === "ellipsis" && e.scrollWidth > e.clientWidth + 1;
    const lc = cs.webkitLineClamp && cs.webkitLineClamp !== "none" && e.scrollHeight > e.clientHeight + 2;
    if((clipX || clipY || ell || lc) && (e.innerText || "").trim() && e.tagName !== "SELECT" && e.id !== "app" && e.id !== "main")
      issue(ell || lc ? "truncated" : "clipped", e, `${ell ? "ellipsis" : lc ? "line clamp" : "overflow hidden"}: content ${e.scrollWidth}x${e.scrollHeight} in box ${e.clientWidth}x${e.clientHeight}`, {full: (e.innerText || "").replace(/\s+/g, " ").trim().slice(0, 400)});
    // Scrollable inner boxes (hidden scroll)
    if((cs.overflowY === "auto" || cs.overflowY === "scroll") && e.scrollHeight > e.clientHeight + 2 && e !== document.documentElement && e !== document.body)
      issue("inner_scroll", e, `inner box scrolls: ${e.scrollHeight}px of content in ${e.clientHeight}px`);
    if(!own) continue;
    // Text under something else (a frame over a frame), on what can be seen.
    if(!modal){
      const vr = visRect(e);
      if(vr.width > 2 && vr.height > 2){
        const at = document.elementFromPoint(vr.left + vr.width / 2, vr.top + vr.height / 2);
        if(at && !(at === e || e.contains(at) || at.contains(e))) issue("text_covered", e, "its text is under " + sel(at));
      }
    }
    const fs = parseFloat(cs.fontSize);
    const minFs = onShow && !e.closest("#sheet") ? 20 : 16;
    // The brand's kicker is decoration (its words are the logo's alt text).
    if(fs < minFs && !e.closest("[data-brand]")) issue("small_font", e, `${fs}px (min ${minFs})`, {own: own.slice(0, 120)});
    const fg = (cs.color.match(/rgba?\(([^)]+)\)/) || [0, "0,0,0"])[1].split(",").map(Number);
    const alpha = fg.length > 3 ? fg[3] : 1;
    const op = Number(cs.opacity);
    let eff = 1; for(let p = e; p; p = p.parentElement) eff *= Number(getComputedStyle(p).opacity);
    const b = bg(e);
    const f = fg.slice(0, 3).map((v, i) => v * alpha * eff + b[i] * (1 - alpha * eff));
    const cr = contrast(f, b);
    const large = fs >= 24 || (fs >= 18.66 && Number(cs.fontWeight) >= 700);
    const disabledCtl = e.closest("button:disabled");
    if(cr < (large ? 3 : 4.5) && !disabledCtl) issue("low_contrast", e, `contrast ${cr.toFixed(2)}:1 at ${fs}px`, {own: own.slice(0, 120)});
    const key = own + "|" + sel(e);
    if(seen.has(key)) continue; seen.add(key);
    const isCtl = !!e.closest("button, label, [role=button], select");
    out.texts.push({sel: sel(e), text: own.slice(0, 600), words: own.split(/\s+/).filter(Boolean).length, ctl: isCtl, fs: fs,
      title: e.closest("[title]") ? e.closest("[title]").title.slice(0, 600) : ""});
  }
  // Tooltips (hover-only text) on controls
  out.tooltips = [...document.querySelectorAll("[title]")].filter(shown).map(e => ({sel: sel(e), title: e.title, words: e.title.split(/\s+/).filter(Boolean).length})).filter(t => t.title);
  // State of the big controls (enabled/disabled, label)
  out.big = ["b-go", "b-hold", "b-stop", "b-armall", "b-disall", "b-abort"].map(id => { const e = document.getElementById(id);
    return e ? {id, shown: shown(e), disabled: e.disabled, label: label(e), opacity: getComputedStyle(e).opacity, filter: getComputedStyle(e).filter} : null; });
  out.stateword = (document.getElementById("pill-t") || {}).textContent;
  out.next = (document.getElementById("next-t") || {}).textContent;
  return out;
};

const OUTLINE = (issues) => {
  const kinds = {clipped: "#ff3b3b", truncated: "#ff3b3b", overlap: "#ff00ff", abort_overlap: "#ff00ff", abort_over_text: "#ff00ff", covered: "#ff00ff",
    small_target: "#00e5ff", small_critical: "#00e5ff", offscreen_x: "#ffff00"};
  const box = document.createElement("div"); box.id = "__audit"; box.style.cssText = "position:absolute;left:0;top:0;pointer-events:none;z-index:2147483647";
  let n = 0;
  for(const it of issues){
    const c = kinds[it.kind]; if(!c) continue;
    for(const s of it.sel.split("  X  ")){
      let el = null; try{ el = document.querySelector(s); }catch(e){}
      if(!el) continue;
      const r = el.getBoundingClientRect();
      const d = document.createElement("div");
      d.style.cssText = `position:absolute;left:${r.left + scrollX}px;top:${r.top + scrollY}px;width:${r.width}px;height:${r.height}px;outline:3px solid ${c};`;
      const t = document.createElement("span"); t.textContent = it.kind; t.style.cssText = `position:absolute;left:0;top:-14px;font:bold 11px sans-serif;background:${c};color:#000;padding:0 2px`;
      d.appendChild(t); box.appendChild(d); n++;
    }
  }
  document.body.appendChild(box);
  return n;
};

async function act(page, extra){
  if(extra === "alert_next"){ await page.click("#al-next"); }
  if(extra === "help"){ await page.click("#help"); }
  if(extra === "signin"){ await page.click("#user"); }
  if(extra === "coach"){ await page.evaluate(() => coachStart()); }
  if(extra === "expand_alerts"){ const m = await page.$("#al-more"); if(m && await m.isVisible()) await m.click(); }
  if(extra === "info") await page.click("#nav-info");
  if(extra === "stop_confirm"){ await page.click("#b-stop"); }
  if(extra === "abort_first_tap"){ await page.click("#b-abort"); }
  if(extra === "hazer_sheet"){ await page.click("#hz-open"); }
  await page.waitForTimeout(500);
}

module.exports = {MEASURE, OUTLINE, VIEWPORTS};
if(require.main === module) (async () => {
  const browser = await chromium.launch({args: ["--no-sandbox"]});
  const summary = [];
  for(const [w, h] of VIEWPORTS){
    if(ONLY && !ONLY.includes(`${w}x${h}`)) continue;
    for(const [sc, view, extra] of CASES){
      const tag0 = `${sc}__${view}${extra ? "__" + extra : ""}__${w}x${h}`;
      if(fs.existsSync(path.join(RES, tag0 + ".json"))) continue;
      try{
      const ctx = await browser.newContext({viewport: {width: w, height: h}, hasTouch: true, deviceScaleFactor: 1});
      const page = await ctx.newPage();
      const errs = [];
      page.on("pageerror", e => errs.push(e.message));
      const eng = new FakeEngine(sc === "purge" ? "idle_operator" : sc);
      if(sc === "purge") eng.name = "purge";
      await eng.attach(page);
      await page.goto("http://rack.test/remote2?tour=0" + (view === "show" ? "" : "#" + view));
      await page.waitForTimeout(sc === "stale" ? 4500 : 1600);
      if(extra) await act(page, extra);
      const m = await page.evaluate(MEASURE);
      m.errors = errs;
      const tag = `${sc}__${view}${extra ? "__" + extra : ""}__${w}x${h}`;
      // Plain screenshot of the viewport, then an annotated full page if anything failed.
      await page.screenshot({path: path.join(SHOTS, tag + ".png")});
      const bad = m.issues.filter(i => ["clipped", "truncated", "overlap", "abort_overlap", "abort_over_text", "covered", "small_critical", "small_target", "offscreen_x", "offscreen_y", "small_font", "text_covered"].includes(i.kind));
      if(bad.length){
        await page.evaluate(OUTLINE, bad);
        await page.screenshot({path: path.join(SHOTS, tag + "__FAIL.png"), fullPage: true});
      }
      m.tag = tag; m.scenario = sc; m.viewname = view; m.extra = extra || ""; m.w = w; m.h = h; m.posts = eng.posts;
      fs.writeFileSync(path.join(RES, tag + ".json"), JSON.stringify(m, null, 1));
      const counts = {};
      for(const i of m.issues) counts[i.kind] = (counts[i.kind] || 0) + 1;
      summary.push({tag, counts, scroll_excess: Math.max(0, m.scroll_h - h), errors: errs.length});
      console.log(tag, JSON.stringify(counts), errs.length ? "ERR " + errs[0] : "");
      await ctx.close();
      }catch(e){ console.log("FAILED", tag0, e.message); }
    }
  }
  fs.writeFileSync(path.join(RES, "_summary_" + (process.env.ONLY_VP || "all") + ".json"), JSON.stringify(summary, null, 1));
  await browser.close();
})();
