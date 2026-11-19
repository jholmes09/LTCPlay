// Remote access over Tailscale: the SuperUser's card (remote.py; SUPERUSER.md).
// Served ONLY to a SuperUser signed in on the show PC's rack screen
// (GET /api/remote/tailscale-card.js, remote.Remote.get, through
// users.may_manage_tailscale): every other screen gets the answer of a
// route that is not there. remote2.html's loadTailscale loads it once and
// calls window.ltcTailscaleCard(view); it uses the page's own $, esc,
// setText, post, confirmBox, ST, restartWhy, pressRestart and loadTailscale.
// Two kinds of line (Jeff, 2026-11-19): a problem, marked with a warning
// sign, amber and the word "Fix:", that needs someone to act; and info, how
// it works, quieter, on the More line. Problems first.
window.ltcTailscaleCard = function(view){
  let c = $("ts-card");
  if(!c){
    c = document.createElement("div");
    c.className = "card col"; c.id = "ts-card";
    c.innerHTML =
      `<div class="t"><b>Remote access over Tailscale</b><span>Your own device, by its Tailscale address.</span></div>` +
      `<div class="pline"><b id="ts-head"></b><ul id="ts-fix" style="margin:4px 0 0 0;padding:0;list-style:none"></ul></div>` +
      `<div class="pline bad" id="ts-detail" hidden></div>` +
      `<div class="pline" id="ts-more" style="opacity:.85"><svg class="i"><use href="#i-info"/></svg> More: <span id="ts-info"></span><span id="ts-serve">Tailscale Serve and HTTPS stay refused.</span></div>` +
      `<div class="pline" id="ts-facts"></div>` +
      `<div class="pbar"><button class="b green" id="ts-on"><svg class="i"><use href="#i-power"/></svg>Turn on</button><button class="b redline" id="ts-restart" hidden><svg class="i"><use href="#i-reset"/></svg>Restart the engine</button></div>` +
      `<div class="pline warn" id="ts-restart-why" role="status" hidden></div>` +
      `<label class="fl" for="ts-logins"><span>Tailscale logins allowed in, one per line</span></label>` +
      `<textarea class="f" id="ts-logins" rows="3" placeholder="andy.example@gmail.com" autocomplete="off"></textarea>` +
      `<div class="pline" id="ts-pw">Long passwords matter: Tailscale reaches past the venue.</div>` +
      `<div class="pbar"><button class="b green" id="ts-logins-save"><svg class="i"><use href="#i-check"/></svg>Save logins</button></div>` +
      `<div class="pline" id="ts-msg" role="status"></div>`;
    $("ts-slot").appendChild(c);
    $("ts-on").addEventListener("click", async () => {
      const on = !(window.TS_VIEW && window.TS_VIEW.on);
      if(on && !(await confirmBox("Turn on Tailscale access?", "Only the logins listed here get in. Each acts as an iPad, signed in. Applies after a restart.", "Yes, turn on", "green"))) return;
      try{ const j = await post("/api/remote/tailscale", {on:on}); setText($("ts-msg"), j.note || ""); draw(j); }
      catch(e){ setText($("ts-msg"), e.message); }
    });
    $("ts-logins-save").addEventListener("click", async () => {
      const logins = $("ts-logins").value.split(/\s+/).map(x => x.trim()).filter(Boolean);
      try{ const j = await post("/api/remote/tailscale", {logins:logins}); setText($("ts-msg"), j.note || ""); $("ts-logins").dataset.drawn = ""; draw(j); }
      catch(e){ setText($("ts-msg"), e.message); }
    });
    $("ts-restart").addEventListener("click", async () => {
      const sure = await confirmBox("Restart the engine?", "Only between shows, every flame group disarmed. flamesafe and the Stream Deck keep running.", "Yes, restart", "green");
      if(!sure) return;
      // The page's own press: the refusal at press time is said and the
      // card redrawn; a press that went waits for the engine to answer
      // again, then redraws the card.
      pressRestart(t => setText($("ts-msg"), t), loadTailscale);
    });
  }
  function draw(v){
    window.TS_VIEW = v || {};
    const V = window.TS_VIEW;
    setText($("ts-head"), V.head || "");
    const fix = (V.problems || []).slice(0, 3);
    const info = (V.info || []).slice(0, Math.max(0, 3 - fix.length));
    $("ts-fix").innerHTML = fix.length
      ? fix.map(b => `<li style="color:#FFD08A;margin:3px 0"><svg class="i"><use href="#i-warn"/></svg> <b style="color:#FFD08A">Fix:</b> ${esc(b)}</li>`).join("")
      : `<li style="margin:3px 0"><svg class="i"><use href="#i-check"/></svg> Nothing to fix.</li>`;
    $("ts-detail").hidden = !V.detail;
    setText($("ts-detail"), V.detail || "");
    setText($("ts-info"), info.map(b => b + " ").join(""));
    const fw = (V.firewall || {}).state || "";
    const fwWord = fw === "present" ? "yes" : fw === "none" ? "not needed" : fw ? "no" : "not checked";
    // The number address first: it works in Chrome today. The name is
    // kept, second and labelled: Chrome tries HTTPS first on a name and
    // refuses it, and LTC Player answers plain HTTP (2026-11-19).
    $("ts-facts").innerHTML = [
      V.url ? `Type this: <b>${esc(V.url)}</b>` : "Type this: none yet",
      V.name_url ? `Name: ${esc(V.name_url)} <span style="opacity:.85">(needs HTTPS: Chrome refuses it for now)</span>` : "Name: none yet",
      "Listening: " + (V.listening ? "yes" : "no"),
      "Firewall rule: " + fwWord,
      "Logins allowed: " + ((V.logins || []).length || "none")].map(t => `<div>${t}</div>`).join("");
    $("ts-on").innerHTML = `<svg class="i"><use href="#i-power"/></svg>` + (V.on ? "Turn off" : "Turn on");
    $("ts-on").className = "b " + (V.on ? "redline" : "green");
    if(V.on) $("ts-on").dataset.safe = ""; else delete $("ts-on").dataset.safe;
    const r = V.restart || {};
    $("ts-restart").hidden = !r.needed;
    $("ts-restart").disabled = !r.offered;
    $("ts-restart").title = r.why || "";
    if(typeof restartWhy === "function") restartWhy("ts-restart", "ts-restart-why", r.offered, r.why);
    // Never over a list being typed: only while it still shows what was
    // drawn last (the card is drawn again after a restart).
    const lg = $("ts-logins"), want = (V.logins || []).join("\n");
    if(document.activeElement !== lg && (!lg.dataset.drawn || lg.value === lg.dataset.drawn)){ lg.value = want; lg.dataset.drawn = want; }
  }
  draw(view);
};
