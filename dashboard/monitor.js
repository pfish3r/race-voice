/* Read-only live monitor. A short-lived ticket is obtained through admin login. */
(()=>{
 const el=id=>document.getElementById(id);
 let socket=null,context=null,gain=null,active=false,starting=false,generation=0;
 let next=[0,0],nodes=[new Set(),new Set()],lastAudio=0,frames=0;
 const text=value=>{if(el("listenStatus").textContent!==value)el("listenStatus").textContent=value};
 function clearAudio(){
  for(const group of nodes){for(const node of group){try{node.stop()}catch{}node.disconnect()}group.clear()}
  next=[0,0];
 }
 function stop(message="Listening stopped"){
  ++generation;active=false;starting=false;
  const old=socket;socket=null;if(old)old.close();
  clearAudio();
  const oldContext=context;context=null;gain=null;
  if(oldContext)oldContext.close().catch(()=>{});
  if(window.speechSynthesis)window.speechSynthesis.cancel();
  el("listen").textContent="Listen live";el("listen").setAttribute("aria-pressed","false");
  el("listenLevel").value=0;text(message);
 }
 function volume(){
  const value=el("listenMute").checked?0:Number(el("listenVolume").value);
  if(gain)gain.gain.value=value;
  return value;
 }
 function packet(buffer){
  if(!active||!context||context.state!=="running"||buffer.byteLength!==1921)return;
  const view=new DataView(buffer),stream=view.getUint8(0);
  if(stream>1)return;
  const now=context.currentTime;
  // Discard scheduled backlog before it can turn into delayed announcements.
  if(next[stream]-now>.20){
   for(const node of nodes[stream]){try{node.stop()}catch{}node.disconnect()}
   nodes[stream].clear();next[stream]=0;
  }
  const audio=context.createBuffer(1,960,48000),samples=audio.getChannelData(0);
  let peak=0;
  for(let i=0;i<960;i++){samples[i]=view.getInt16(1+i*2,true)/32768;peak=Math.max(peak,Math.abs(samples[i]))}
  const source=context.createBufferSource();source.buffer=audio;source.connect(gain);
  const when=Math.max(now+.04,next[stream]);
  source.start(when);next[stream]=when+.02;nodes[stream].add(source);
  source.onended=()=>{nodes[stream].delete(source);source.disconnect()};
  el("listenLevel").value=peak*100;
  lastAudio=Date.now();frames++;
  text(el("listenMute").checked?"Receiving audio - muted":"Receiving live audio");
 }
 function announcement(message){
  if(!active)return;
  if(message.type==="stop"){window.speechSynthesis?.cancel();el("listenAnnouncement").textContent="Announcement stopped";return}
  if(message.type!=="say"||typeof message.text!=="string")return;
  el("listenAnnouncement").textContent="Last announcement: "+message.text;
  const synth=window.speechSynthesis;if(synth)synth.cancel();
  if(!el("listenSpeech").checked||volume()===0)return;
  const voice=synth?.getVoices().find(v=>v.localService&&v.lang.toLowerCase().startsWith("en"))||synth?.getVoices().find(v=>v.localService);
  if(!voice){el("listenAnnouncement").textContent+=" (No local browser voice available.)";return}
  const speech=new SpeechSynthesisUtterance(message.text);
  speech.voice=voice;speech.volume=volume();
  speech.onerror=()=>{if(active)el("listenAnnouncement").textContent+=" (Browser speech unavailable.)"};
  synth.speak(speech);
 }
 async function start(){
  if(active||starting){stop();return}
  const tokenGeneration=++generation;
  starting=true;frames=0;lastAudio=0;
  el("listen").textContent="Stop listening";el("listen").setAttribute("aria-pressed","true");
  text("Enabling browser audio...");
  try{
   const Audio=window.AudioContext||window.webkitAudioContext;
   if(!Audio)throw Error("This browser does not support Web Audio");
   context=new Audio({latencyHint:"interactive"});
   gain=context.createGain();gain.connect(context.destination);volume();
   await context.resume();
   if(tokenGeneration!==generation)return;
   if(context.state!=="running")throw Error("Audio is blocked. Enable sound for this site and try again");
   context.onstatechange=()=>{
    if(tokenGeneration===generation&&active&&context&&context.state!=="running")stop("Browser paused audio. Click Listen live to resume.");
   };
   const response=await fetch("/admin/api/monitor-ticket",{cache:"no-store",signal:AbortSignal.timeout(10000)});
   if(tokenGeneration!==generation)return;
   if(!response.ok)throw Error("Could not authorise listening (HTTP "+response.status+")");
   const data=await response.json();
   if(tokenGeneration!==generation)return;
   socket=new WebSocket("wss://"+location.host+"/admin/monitor");
   socket.binaryType="arraybuffer";
   const ws=socket;
   let timer=setTimeout(()=>{if(tokenGeneration===generation)stop("Listener connection timed out. Click Listen live to retry.")},10000);
   ws.onopen=()=>{
    if(tokenGeneration!==generation){ws.close();return}
    ws.send(JSON.stringify({role:"monitor",key:data.ticket}));
    data.ticket="";
    text("Authenticating listener...");
   };
   ws.onmessage=e=>{
    if(tokenGeneration!==generation)return;
    if(e.data instanceof ArrayBuffer){packet(e.data);return}
    try{
     const message=JSON.parse(e.data);
     if(message.type==="monitor_ready"){
      clearTimeout(timer);active=true;starting=false;
      text("Listening - waiting for sender audio");
     }else announcement(message);
    }catch{stop("Invalid listener response. Reconnect to try again.")}
   };
   ws.onerror=()=>{if(tokenGeneration===generation)text("Listener connection error. Check relay connectivity.")};
   ws.onclose=e=>{
    clearTimeout(timer);
    if(tokenGeneration===generation)stop("Listening disconnected"+(e.reason?": "+e.reason:". Click Listen live to reconnect."));
   };
  }catch(e){if(tokenGeneration===generation)stop(e.message)}
 }
 el("listen").addEventListener("click",start);
 el("listenVolume").addEventListener("input",()=>{volume();window.speechSynthesis?.cancel()});
 el("listenMute").addEventListener("change",()=>{volume();window.speechSynthesis?.cancel()});
 el("listenSpeech").addEventListener("change",()=>{if(!el("listenSpeech").checked)window.speechSynthesis?.cancel()});
 window.addEventListener("pagehide",()=>stop());
 setInterval(()=>{
  if(active&&(!lastAudio||Date.now()-lastAudio>2000)){
   el("listenLevel").value=0;text("Listening - no recent audio. Enable system audio or hold push-to-talk on the sender.");
  }
  el("listenFrames").textContent=frames+" audio frames received in this listening session";
 },1000);
})();
