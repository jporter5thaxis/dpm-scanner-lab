const el = id => document.getElementById(id);
let devices = [], records = [], selected = [], session = null, busy = false;
let localStream, localName, localFps = 0;
const browserMode = () => el("location").value === "browser";
const browserModes = [
  {label:"Camera default", constraints:{}},
  ...[[1280,720],[1920,1080],[3840,2160],[8000,6000]].map(([width,height]) => ({
    label:width+" × "+height+" preferred", constraints:{width:{ideal:width},height:{ideal:height}}
  }))
];
function releaseBrowser() {
  localStream?.getTracks().forEach(track => track.stop());
  localStream=null;
  el("local").srcObject=null;
  localFps=0;
}
async function discoverBrowser() {
  if (!navigator.mediaDevices) throw Error("Open this lab on localhost or HTTPS to access browser cameras.");
  const cameras=(await navigator.mediaDevices.enumerateDevices()).filter(d=>d.kind==="videoinput");
  const previous=el("device").value;
  el("device").replaceChildren(...cameras.map((d,i)=>new Option(d.label || "Camera "+(i+1), d.deviceId)));
  if (!cameras.length) el("device").add(new Option("Default camera",""));
  if ([...el("device").options].some(o=>o.value===previous)) el("device").value=previous;
  modes();
}
async function startBrowser() {
  releaseBrowser();
  const id=el("device").value, mode=browserModes[el("mode").selectedIndex];
  const video=el("local");
  const stream=await navigator.mediaDevices.getUserMedia({audio:false,video:{
    ...mode.constraints,...(id?{deviceId:{exact:id}}:{})
  }});
  localStream=stream;
  const track=stream.getVideoTracks()[0];
  try {
    video.srcObject=stream;
    await video.play();
    localName=track.label || "Browser camera";
    await discoverBrowser();
    el("device").value=track.getSettings().deviceId;
    el("mode").selectedIndex=browserModes.indexOf(mode);
  } catch(e) {releaseBrowser();throw e;}
  let startTime=0, firstFrame=0;
  function measure(now, frame) {
    if (localStream!==stream) return;
    if (!startTime) {startTime=now;firstFrame=frame.presentedFrames;}
    if (now-startTime>=1000) {
      localFps=Math.round((frame.presentedFrames-firstFrame)*10000/(now-startTime))/10;
      startTime=now;firstFrame=frame.presentedFrames;
    }
    video.requestVideoFrameCallback(measure);
  }
  video.requestVideoFrameCallback?.(measure);
}
async function saveBrowser() {
  const video=el("local"), canvas=document.createElement("canvas");
  canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  canvas.getContext("2d").drawImage(video,0,0);
  const png=await new Promise(resolve=>canvas.toBlob(resolve,"image/png"));
  if (!png) throw Error("Browser could not save this frame.");
  const params=new URLSearchParams({camera:localName, note:el("note").value.trim(), fps:localFps});
  const response=await fetch("/api/browser-shot?"+params,{method:"POST",headers:{"Content-Type":"image/png"},body:png});
  const result=await response.json();
  if (!response.ok) throw Error(result.detail || "Saving the frame failed.");
  return result;
}
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
  if (browserMode()) {
    el("mode").replaceChildren(...browserModes.map(m=>new Option(m.label)));
    el("start").disabled=busy;return;
  }
  const camera = devices[el("device").selectedIndex];
  el("mode").replaceChildren(...(camera?.modes || []).map(m => new Option(m.width+" × "+m.height+" · "+m.fps+" fps · "+m.fourcc)));
  el("start").disabled = !camera || busy;
}
async function discover() {
  if(browserMode()) return discoverBrowser();
  const previous = el("device").value;
  devices = await api("devices");
  el("device").replaceChildren(...devices.map(d => new Option(d.name+" · video"+d.device, d.device)));
  if (devices.some(d=>String(d.device)===previous)) el("device").value=previous;
  modes();
  if (!devices.length) el("notice").textContent="No supported capture devices found. Check the camera connection and container device mappings.";
}
async function status() {
  if (browserMode()) {
    const video=el("local");
    const live=!!(localStream?.active && video.readyState>=2);
    video.hidden=!live;el("idle").hidden=live;el("feed").hidden=true;
    el("idle").textContent="Select a camera and start the preview.";
    el("source").textContent=live?localName:"Camera idle";
    el("stats").textContent=live?video.videoWidth+" × "+video.videoHeight+" · "+localFps+" preview fps":"";
    el("capture").disabled=!live||busy;el("stop").disabled=!localStream||busy;
    return;
  }
  el("local").hidden=true;
  const s = await api("status"), c = s.camera;
  if (browserMode()) return;
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
  finally {busy=false; el("start").disabled=!browserMode() && !devices.length; await status().catch(e=>{el("notice").textContent=e.message;});}
}
el("refresh").onclick=()=>action(discover);
el("location").onchange=()=>action(async()=>{
  releaseBrowser();
  if (session) {await api("stop",{});session=null;el("feed").removeAttribute("src");}
  await discover();
});
el("device").onchange=modes;
el("detail").onchange=renderShots;
el("start").onclick=()=>action(async()=>{
  if(browserMode()) return startBrowser();
  const d=devices[el("device").selectedIndex], m=d.modes[el("mode").selectedIndex];
  await api("start",{device:d.device,name:d.name,...m});
});
el("stop").onclick=()=>action(()=>browserMode()?releaseBrowser():api("stop",{}));
el("capture").onclick=()=>action(async()=>{
  const s=browserMode()?await saveBrowser():await api("shots",{session,note:el("note").value.trim()});
  records.unshift(s); selected=[...selected.slice(-2),s.id];renderShots();
  el("notice").textContent="Test shot saved.";
});
await action(async()=>{await discover();records=await api("shots");selected=records.slice(0,3).map(s=>s.id);renderShots();});
async function poll(){try {await status();}catch(e){el("notice").textContent=e.message;}setTimeout(poll,1000);}
poll();
