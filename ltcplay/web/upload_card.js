// The nightly upload's card (users.py; USERS.md, "The SuperUser").
// Served ONLY to a SuperUser signed in on the show PC's rack screen
// (GET /api/remote/upload-card.js, remote.Remote.get): every other screen
// gets the answer of a route that is not there, so this card's words and
// routes never reach a screen that cannot use them. remote2.html's
// renderUpload loads it once and calls window.ltcUploadCard(UV); it uses
// the page's own $, post, uMsg and loadUsers.
window.ltcUploadCard = function(UV){
  const u = (UV.upload && UV.upload.upload) || {};
  let c = $("u-upload");
  if(!c){
    c = document.createElement("section");
    c.className = "card"; c.id = "u-upload";
    c.innerHTML = `<h2>Nightly upload</h2>` +
      `<ul class="dim" style="font-size:13px;margin:0 0 6px 18px;padding:0"><li>SuperUser only, on this show PC.</li><li>Every change goes in the audit log.</li><li>Saving asks for your password again.</li></ul>` +
      `<div class="kv" id="au-up"></div>` +
      `<label><input type="checkbox" id="au-en"> Send the audit log to the server every night</label>` +
      `<label for="au-url">Server address (https://)</label>` +
      `<input id="au-url" placeholder="https://audit.example.com/ltc">` +
      `<label for="au-tok">Shared token</label>` +
      `<input id="au-tok" type="password" autocomplete="off" placeholder="unchanged">` +
      `<div class="three"><div><label for="au-time">Time of day</label><input id="au-time" placeholder="03:30"></div>` +
      `<div><label for="au-keep">Keep days here</label><input id="au-keep" type="number" min="90"></div></div>` +
      `<label for="au-pw">Your password, to save</label>` +
      `<input id="au-pw" type="password" autocomplete="current-password">` +
      `<div class="row" style="margin-top:8px"><button id="au-upsave">Save</button><button class="ghost" id="au-now">Send now</button></div>` +
      `<div class="toast" id="au-upmsg"></div>`;
    const a = $("u-audit");
    a.parentNode.insertBefore(c, a.nextSibling);
    $("au-upsave").addEventListener("click", async () => {
      const body = {enabled:$("au-en").checked, url:$("au-url").value.trim() || undefined, time:$("au-time").value.trim(),
        keep_days:parseInt($("au-keep").value, 10) || 90, password:$("au-pw").value};
      if($("au-tok").value) body.token = $("au-tok").value;
      if(body.url === undefined) delete body.url;
      try{ const r = await post("/api/remote/audit-settings", body); uMsg("au-upmsg", r.text); }
      catch(e){ uMsg("au-upmsg", e.message, true); }
      $("au-tok").value = ""; $("au-pw").value = "";
      loadUsers();
    });
    $("au-now").addEventListener("click", async () => {
      try{ const r = await post("/api/remote/audit-upload-now", {}); uMsg("au-upmsg", r.text); }catch(e){ uMsg("au-upmsg", e.message, true); }
    });
  }
  $("au-up").textContent = (u.enabled ? ("On, to " + (u.host || "?") + " at " + u.time + ".") : "Off.") +
    " Last upload: " + (u.last_upload_at || "never") + ". Days waiting: " + ((u.pending_days || []).join(", ") || "none") + "." +
    (u.last_error ? " Last error: " + u.last_error : "") + (u.config_error ? " Settings problem: " + u.config_error : "") +
    ((u.changed_since_sent || []).length ? " CHANGED SINCE SENT: " + u.changed_since_sent.join(", ") : "");
  if(!(document.activeElement && c.contains(document.activeElement))){
    $("au-en").checked = !!u.enabled;
    $("au-time").value = u.time || "03:30";
    $("au-keep").value = u.keep_days || 90;
    $("au-tok").placeholder = u.token_set ? "saved (type to replace)" : "not set";
  }
};
