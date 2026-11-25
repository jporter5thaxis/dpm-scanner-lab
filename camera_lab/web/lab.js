const el = id => document.getElementById(id);
let devices = [], records = [], selected = [], session = null, busy = false;
async function api(path, body) {
  const options = body === undefined ? {} : {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)};
  const response = await fetch("/api/" + path, options);
  const result = await response.json();
  if (!response.ok) throw Error(typeof result.detail === "string" ? result.detail : "Request failed.");
  return result;
}
function text(tag, value) {
  const node = document.createElement(tag);
  node.textContent = value;
  return node;
}
function modes() {
  const camera = devices[el("device").selectedIndex];
  el("mode").replaceChildren(...(camera?.modes || []).map(m => new Option(m.width+" × "+m.height+" · "+m.fps+" fps · "+m.fourcc)));
  el("start").disabled = !camera || busy;
}
async function discover() {
  const previous = el("device").value;
  const report = await api("driver-report");
  devices = report.nodes.filter(node => node.modes.length);
  el("report").textContent = report.nodes.map(node => [
    node.path, node.name, node.driver, node.bus, node.issue, node.formats, node.controls
  ].filter(Boolean).join("\n")).join("\n\n") || "No video device is mapped into this container.";
  el("device").replaceChildren(...devices.map(d => new Option(d.name+" · video"+d.device, d.device)));
  if (devices.some(d=>String(d.device)===previous)) el("device").value=previous;
  modes();
  if (!devices.length) el("notice").textContent="No supported capture devices found. Check the camera connection and container device mappings.";
}
async function status() {
  const s = await api("status"), c = s.camera;
  if (session !== s.session) {
    session = s.session;
    if (session) el("feed").src="/api/preview/"+session;
    else el("feed").removeAttribute("src");
  }
  const live = session && c.state==="RUNNING" && c.age_ms!==null && c.age_ms<2000;
  el("feed").hidden=!live;
  el("idle").hidden=!!live;
  el("idle").textContent=c.error || (session ? "Waiting for camera frames…" : "Select a camera and start the preview.");
  el("source").textContent=s.selection?.name || "Camera idle";
  el("stats").textContent=live ? c.resolution.join(" × ")+" · "+c.fps+" capture fps" : c.state;
  el("capture").disabled=!live || busy;
  el("stop").disabled=!session || busy;
  const scan = s.scan;
  el("strategy").disabled = !!scan || busy;
  if (scan) el("strategy").value = scan.center_first ? "center" : "full";
  el("scan").disabled = busy || (!live && !scan);
  el("scan").setAttribute("aria-pressed", String(!!scan));
  el("scan").textContent = scan ? "Stop QR reading" : "Enable QR reading";
  const lines = [];
  if (scan) {
    lines.push(scan.error || (scan.latest ? scan.latest.symbology+" · "+scan.latest.value : "Looking for QR or Data Matrix…"));
    if (scan.first_read_ms !== null) lines.push("First read: "+scan.first_read_ms+" ms");
    if (scan.first_confirmed_ms !== null) lines.push("First confirmation: "+scan.first_confirmed_ms+" ms");
    if (scan.count) lines.push("Confirmed scans: "+scan.count);
  }
  const summary = lines.join("\n");
  if (el("scan-result").textContent !== summary) el("scan-result").textContent = summary;
}
function renderShots() {
  const history=el("history"), cards=el("cards");
  history.replaceChildren();
  cards.replaceChildren();
  if (!records.length) history.append(text("p","Your saved test shots will appear here."));
  for (const s of records) {
    const label=text("label",""); label.className="row";
    const check=document.createElement("input"); check.type="checkbox";
    check.checked=selected.includes(s.id);
    check.disabled=!check.checked && selected.length>=3;
    check.onchange=()=>{selected=check.checked?[...selected,s.id]:selected.filter(id=>id!==s.id);renderShots();};
    label.append(check,text("span",[s.camera,s.note,new Date(s.captured).toLocaleString()].filter(Boolean).join(" · ")));
    history.append(label);
  }
  for (const id of selected) {
    const s=records.find(r=>r.id===id); if(!s) continue;
    const base="/api/shots/"+id+"/", card=text("article",""), info=text("div","");
    const img=document.createElement("img"), detail=el("detail").checked;
    img.src=base+(detail?"detail.png":"preview.jpg");img.className=detail?"detail":"";img.alt=s.camera+" test shot";
    const original=text("a","Open original PNG"); original.href=base+"original.png";original.target="_blank";original.rel="noopener";
    info.append(text("strong",s.camera),text("p",s.note),text("p",s.width+" × "+s.height+" · "+s.fps+(s.requested.source==="browser"?" preview fps":" capture fps")),
      text("p","Sharpness "+s.sharpness+" · Contrast "+s.contrast),text("p","Highlights clipped "+(100*s.glare_fraction).toFixed(1)+"% · Shadows clipped "+(100*s.dark_fraction).toFixed(1)+"%"),original);
    card.append(img,info);cards.append(card);
  }
}
async function action(fn) {
  if(busy)return;
  busy=true;el("notice").textContent="";
  try {await fn();} catch(e){el("notice").textContent=e.message;}
  finally {busy=false; el("start").disabled=!devices.length; await status().catch(e=>{el("notice").textContent=e.message;});}
}
el("refresh").onclick=()=>action(discover);
el("device").onchange=modes;
el("detail").onchange=renderShots;
el("start").onclick=()=>action(async()=>{
  const d=devices[el("device").selectedIndex], m=d.modes[el("mode").selectedIndex];
  await api("start",{device:d.device,name:d.name,...m});
});
el("stop").onclick=()=>action(()=>api("stop",{}));
el("scan").onclick=()=>action(()=>api("scan",{
  session, enabled:el("scan").getAttribute("aria-pressed")!=="true", center_first:el("strategy").value==="center"
}));
el("capture").onclick=()=>action(async()=>{
  const s=await api("shots",{session,note:el("note").value.trim()});
  records.unshift(s); selected=[...selected.slice(-2),s.id];renderShots();
  el("notice").textContent="Test shot saved.";
});
await action(async()=>{await discover();records=await api("shots");selected=records.slice(0,3).map(s=>s.id);renderShots();});
let historyKey;
async function refreshHistory() {
  const repeats = el("repeats").checked;
  const rows = await api("scan-history?repeats="+repeats);
  if (repeats !== el("repeats").checked) return;
  const key = [repeats, rows[0]?.id, rows.length].join("/");
  if (key === historyKey) return;
  historyKey = key;
  const list = el("readings");
  const items = rows.map(row => {
    const item = document.createElement("li");
    const clock = new Date(row.at).toLocaleTimeString([], {
      hour:"2-digit", minute:"2-digit", second:"2-digit", fractionalSecondDigits:3, hour12:false
    });
    item.append(text("small",clock+" · "+row.stage), text("strong", row.value),
      text("span",row.symbology+" · "+row.method),
      text("span",row.path),
      text("span","Trial "+row.trial_ms+" ms · Frame "+row.frame_ms+" ms"));
    return item;
  });
  list.replaceChildren(...items);
  if (!rows.length) list.append(text("li","Waiting for a reading."));
}
async function poll(){
  try {await Promise.all([status(), refreshHistory()]);} catch(e){el("notice").textContent=e.message;}
  setTimeout(poll,500);
}
poll();
