// Remote access over Tailscale: the SuperUser's card (remote.py; SUPERUSER.md).
// Served ONLY to a SuperUser signed in on the show PC's rack screen
// (GET /api/remote/tailscale-card.js, remote.Remote.get, through
// users.may_manage_tailscale): every other screen gets the answer of a
// route that is not there. remote2.html's loadTailscale loads it once and
// calls window.ltcTailscaleCard(view); it uses the page's own $, esc,
// setText, post, confirmBox and ST.
window.ltcTailscaleCard = function(view){
  let c = $("ts-card");
  if(!c){
    c = document.createElement("div");
    c.className = "card col"; c.id = "ts-card";
    c.innerHTML =
      `<div class="t"><b>Remote access over Tailscale</b><span>Your own device, by its Tailscale name.</span></div>` +
      `<div class="pline"><b id="ts-head"></b><ul id="ts-bul" style="margin:4px 0 0 18px;padding:0"></ul></div>` +
      `<div class="pline" id="ts-facts"></div>` +
      `<div class="pline" id="ts-serve">Tailscale Serve and HTTPS stay refused.</div>` +
      `<div class="pbar"><button class="b green" id="ts-on"><svg class="i"><use href="#i-power"/></svg>Turn on</button><button class="b redline" id="ts-restart" hidden><svg class="i"><use href="#i-reset"/></svg>Restart the engine</button></div>` +
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
      try{ const j = await post("/api/remote/tailscale", {logins:logins}); setText($("ts-msg"), j.note || ""); draw(j); }
      catch(e){ setText($("ts-msg"), e.message); }
    });
    $("ts-restart").addEventListener("click", async () => {
      const sure = await confirmBox("Restart the engine?", "Only between shows, every flame group disarmed. flamesafe and the Stream Deck keep running.", "Yes, restart", "green");
      if(!sure) return;
      try{ const j = await post("/api/remote/reload-engine", {}); setText($("ts-msg"), j.text); }
      catch(e){ setText($("ts-msg"), e.message); }
    });
  }
  function draw(v){
    window.TS_VIEW = v || {};
    const V = window.TS_VIEW;
    setText($("ts-head"), V.head || "");
    $("ts-bul").innerHTML = (V.bullets || []).slice(0, 3).map(b => `<li>${esc(b)}</li>`).join("");
    const fw = (V.firewall || {}).state || "";
    const fwWord = fw === "present" ? "yes" : fw === "none" ? "not needed" : fw ? "no" : "not checked";
    $("ts-facts").innerHTML = [
      V.name_url ? `URL: <b>${esc(V.name_url)}</b>` : "URL: none yet",
      V.url ? `Address: <b>${esc(V.url)}</b>` : "Address: none yet",
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
    if(document.activeElement !== $("ts-logins")) $("ts-logins").value = (V.logins || []).join("\n");
  }
  draw(view);
};
