const $=id=>document.getElementById(id);
const show=(id,value)=>$(id).textContent=value??"-";
const date=t=>t?new Date(t*1000).toLocaleString():"-";
const bytes=n=>(n/1048576).toFixed(2)+" MiB";
let previous=null, busy=false, clientLogs=[];
const clientEventNames={
 speech_queued:"Announcement queued",speech_started:"Speech started",
 speech_completed:"Speech completed",speech_cancelled:"Speech cancelled",
 speech_duplicate:"Duplicate ignored",speech_error:"Speech error",
 speech_rejected:"Announcement rejected",command_sent:"Command sent",
 connected:"Connected",disconnected:"Disconnected",speech_output:"Speech output selected"
};
function renderClientLogs(){
 const target=$("clientLogs"),role=$("clientLogRole").value;target.replaceChildren();
 const visible=clientLogs.filter(e=>role==="all"||e.role===role);
 for(const e of visible){
  const li=document.createElement("li"),label=document.createElement("strong"),meta=document.createElement("span"),stamp=document.createElement("time");
  label.textContent=(e.role==="receiver"?"Receiver":"Sender")+": "+(clientEventNames[e.event]||e.event);
  const parts=[];
  if(e.id)parts.push("ID "+e.id);
  if(Number.isInteger(e.characters))parts.push(e.characters+" characters");
  if(typeof e.seconds==="number")parts.push(e.seconds.toFixed(2)+" seconds");
  if(e.detail)parts.push(e.detail.replaceAll("_"," "));
  if(e.version)parts.push("v"+e.version);
  meta.textContent=parts.join(" | ");meta.className="logmeta";
  stamp.textContent=date(e.time);li.append(label,meta,stamp);target.append(li);
 }
 if(!visible.length){const li=document.createElement("li");li.textContent="No client events yet. Connect an updated Windows client and send an announcement.";target.append(li)}
}
$("clientLogRole").addEventListener("change",renderClientLogs);
$("downloadClientLogs").addEventListener("click",()=>{
 const blob=new Blob([JSON.stringify({exported_at:new Date().toISOString(),events:clientLogs},null,2)],{type:"application/json"});
 const url=URL.createObjectURL(blob),link=document.createElement("a");
 link.href=url;link.download="RaceVoice-Client-Log.json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
function row(parent,label,value){const dt=document.createElement("dt"),dd=document.createElement("dd");dt.textContent=label;dd.textContent=value;parent.append(dt,dd)}
async function refresh(){
 try{
  const response=await fetch("/admin/api/status",{cache:"no-store",signal:AbortSignal.timeout(12000)});
  if(!response.ok)throw Error("Dashboard unavailable (HTTP "+response.status+")");
  const d=await response.json(),r=d.relay,c=d.certificate,t=c.tls||{},j=c.renewal||{};
  show("live","Live");$("live").className="badge";
  show("notice",d.controller_available?"Relay responding. Certificate details are read from the active TLS listener.":"Relay responding, but certificate controller is unavailable.");
  show("hostname",d.hostname);show("endpoint","wss://"+d.hostname+":"+d.port);
  show("uptime",Math.floor(r.uptime_seconds/3600)+"h "+Math.floor(r.uptime_seconds%3600/60)+"m "+r.uptime_seconds%60+"s");
  show("sender",r.sender_connected?"Connected":"Disconnected");
  show("receiver",r.receiver_connected?"Connected":"Disconnected");
  show("senderSince",r.sender_connected?"Since "+date(r.connected_at.sender):"Waiting for the main hall PC");
  show("receiverSince",r.receiver_connected?"Since "+date(r.connected_at.receiver):"Waiting for the other room PC");
  show("enabled",r.receiver_connected&&r.receiver_enabled?"Enabled":"Disabled");
  const now=Date.now(),s=r.stats;
  show("rate",previous&&s.audio_bytes_out>=previous.bytes?((s.audio_bytes_out-previous.bytes)*8/(now-previous.time)).toFixed(1)+" kbps":"-");
  previous={time:now,bytes:s.audio_bytes_out};
  show("certState",!t.available?"Unavailable":!t.trusted?"Verification failed":t.days_remaining<14?"Expiring soon":"Valid");
  $("certState").className="badge"+(!t.available||!t.trusted||t.days_remaining<14?" warn":"");
  show("issuer",t.issuer);show("issued",date(t.issued_at));show("expires",date(t.expires_at));
  show("days",t.available?t.days_remaining+" days":"-");show("names",(t.names||[]).join(", "));
  show("checked",date(t.checked_at));show("fingerprint",t.fingerprint);
  show("automatic",c.automatic_renewal?"Enabled - GoDaddy DNS":"Unknown");
  show("caddy",c.caddy_running?"Running":"Unavailable");
  show("jobState","Manual renewal: "+(j.state||"unknown"));show("jobMessage",j.message||c.message||"No status available");
  const cooldown=c.next_manual_attempt>Date.now()/1000;
  show("nextAttempt",cooldown?"Next manual attempt: "+date(c.next_manual_attempt):"Manual renewal available");
  $("renew").disabled=busy||!t.available||!d.controller_available||cooldown||["running","loading"].includes(j.state);
  const stats=$("stats");stats.replaceChildren();
  [["Admin listeners",r.admin_listeners||0],["Browser audio forwarded",bytes(s.monitor_bytes_out||0)],["Audio received",bytes(s.audio_bytes_in)],["Audio forwarded",bytes(s.audio_bytes_out)],["Frames forwarded",s.frames_forwarded],["Receiver frames dropped",s.frames_dropped],["Announcements forwarded",s.announcements],["Sender connections",r.connections.sender],["Receiver connections",r.connections.receiver],["Authentication rejections",s.auth_rejections],["Last audio received",date(r.last_audio)]].forEach(([a,b])=>row(stats,a,b));
  clientLogs=Array.isArray(r.client_logs)?r.client_logs:[];renderClientLogs();
  const events=$("events");events.replaceChildren();
  for(const e of r.events){const li=document.createElement("li"),time=document.createElement("time");li.textContent=e.message;time.textContent=date(e.time);li.append(time);events.append(li)}
  if(!r.events.length){const li=document.createElement("li");li.textContent="No connection activity yet";events.append(li)}
  show("updated","Updated "+new Date().toLocaleTimeString());
 }catch(e){show("live","Disconnected");$("live").className="badge warn";show("notice","Status is stale: "+e.message+". Retrying automatically.");$("renew").disabled=true;previous=null}
}
$("renew").addEventListener("click",async()=>{
 if(!confirm("Request a new Let's Encrypt certificate now? Successful renewal briefly restarts Caddy and reconnects audio clients. One manual attempt is allowed per 24 hours."))return;
 busy=true;$("renew").disabled=true;
 try{
  const res=await fetch("/admin/api/renew",{method:"POST",headers:{"X-RaceVoice-Action":"renew"},signal:AbortSignal.timeout(15000)});
  const result=await res.json();show("jobMessage",result.message||result.error);
  if(!res.ok)alert(result.message||result.error||"Renewal could not be started");
 }catch(e){alert("Could not confirm the request. Refresh status before trying again.")}
 finally{busy=false;await refresh()}
});
async function poll(){await refresh();setTimeout(poll,3000)}poll();

const keyTimers={};
function hideKey(role){
 clearTimeout(keyTimers[role]);$(role+"Key").value="";$(role+"Key").type="password";
 show(role+"Show","Show");$(role+"Show").setAttribute("aria-expanded","false");
}
async function fetchKey(role){
 const res=await fetch("/admin/api/keys/"+role,{cache:"no-store",signal:AbortSignal.timeout(10000)});
 if(!res.ok)throw Error("Could not load key (HTTP "+res.status+")");
 const data=await res.json();
 if(data.role!==role||typeof data.key!=="string"||!data.key)throw Error("Invalid key response");
 return data.key;
}
for(const role of ["sender","receiver"]){
 $(role+"Show").addEventListener("click",async()=>{
  if($(role+"Key").type==="text"){hideKey(role);return}
  $(role+"Show").disabled=true;
  try{
   const key=await fetchKey(role);
   if(document.hidden)return;
   $(role+"Key").type="text";$(role+"Key").value=key;
   show(role+"Show","Hide");$(role+"Show").setAttribute("aria-expanded","true");
   keyTimers[role]=setTimeout(()=>hideKey(role),30000);
   show("keyMessage",role==="sender"?"Sender key shown for the main hall PC.":"Receiver key shown for the other room PC.");
  }catch(e){show("keyMessage",e.message)}
  finally{$(role+"Show").disabled=false}
 });
 $(role+"Copy").addEventListener("click",async()=>{
  $(role+"Copy").disabled=true;
  try{
   const key=await fetchKey(role);
   await navigator.clipboard.writeText(key);
   show("keyMessage",(role==="sender"?"Sender":"Receiver")+" key copied. Paste it into the Windows app Access key field.");
  }catch(e){show("keyMessage","Copy failed. Click Show, select the key and copy it manually.")}
  finally{$(role+"Copy").disabled=false}
 });
}
document.addEventListener("visibilitychange",()=>{if(document.hidden){hideKey("sender");hideKey("receiver")}});
