/* Server-retained metrics. No external chart library or third-party requests. */
(() => {
"use strict";
const $ = id => document.getElementById(id), NS = "http://www.w3.org/2000/svg";
let data = null;
const number = n => Number.isFinite(n) ? n.toLocaleString(undefined, {maximumFractionDigits: 1}) : "-";
const mib = n => Number.isFinite(n) ? number(n / 1048576) + " MiB" : "Unavailable";
const text = (id, value) => { $(id).textContent = value; };
const counter = (s, k) => s.counters[k] ?? 0;
const rate = (s, k) => s.rates[k] ?? null;
const multiply = (n, k) => n == null ? null : n * k;
const colors = ["#91ceff", "#ffc872", "#c8a8ff"];
const charts = [
 {id:"bandwidthChart", title:"Bandwidth", unit:"kbps", series:[
  ["Received",s=>multiply(rate(s,"payload_bytes_in"),.008)],
  ["Sent",s=>multiply(rate(s,"payload_bytes_out"),.008)]]},
 {id:"dataChart", title:"Data used since relay restart", unit:"MiB", series:[
  ["Received",s=>counter(s,"payload_bytes_in")/1048576],
  ["Sent",s=>counter(s,"payload_bytes_out")/1048576]]},
 {id:"eventChart", title:"Announcement and STOP events", unit:"events/min", series:[
  ["Relay received",s=>multiply(rate(s,"events_received"),60)],
  ["Receiver forwarded",s=>multiply(rate(s,"events_forwarded"),60)]]},
 {id:"dropChart", title:"Audio frames dropped", unit:"frames/min", series:[
  ["Receiver",s=>multiply(rate(s,"frames_dropped"),60)],
  ["Browser listeners",s=>multiply(rate(s,"monitor_frames_dropped"),60)]]},
 {id:"cpuChart", title:"Container CPU", unit:"% of one core", series:[
  ["Relay",s=>s.containers.relay.cpu_percent],
  ["Caddy",s=>s.containers.caddy.cpu_percent]]},
 {id:"memoryChart", title:"Container memory", unit:"MiB", series:[
  ["Relay",s=>multiply(s.containers.relay.memory_bytes,1/1048576)],
  ["Caddy",s=>multiply(s.containers.caddy.memory_bytes,1/1048576)]]}
];
function svgNode(tag, attrs={}, value) {
 const el = document.createElementNS(NS,tag);
 for(const [key,val] of Object.entries(attrs)) el.setAttribute(key,String(val));
 if(value !== undefined) el.textContent = value;
 return el;
}
function chart(config, samples, start, end) {
 const target = $(config.id);
 target.replaceChildren();
 const values = config.series.map(([,get]) => samples.map(get));
 const finite = values.flat().filter(Number.isFinite);
 const max = Math.max(.1, ...finite) * 1.12;
 const x = t => 58 + (t-start)/(end-start)*524;
 const y = v => 174-v/max*144;
 const svg = svgNode("svg",{viewBox:"0 0 600 210",role:"img","aria-label":config.title});
 svg.append(svgNode("title",{},config.title + ". " + (samples.length ? config.series.map(([name],i) =>
  name+": "+number(values[i].at(-1))+" "+config.unit).join("; ") : "Waiting for samples")));
 for(let i=0;i<4;i++){
  const value = max*i/3, yy = y(value);
  svg.append(svgNode("line",{x1:58,y1:yy,x2:582,y2:yy,class:"chart-grid"}));
  svg.append(svgNode("text",{x:50,y:yy+4,"text-anchor":"end",class:"chart-label"},number(value)));
 }
 for(const [t,anchor] of [[start,"start"],[(start+end)/2,"middle"],[end,"end"]])
  svg.append(svgNode("text",{x:x(t),y:198,"text-anchor":anchor,class:"chart-label"},
   new Date(t*1000).toLocaleTimeString([], {hour:"2-digit",minute:"2-digit"})));
 config.series.forEach(([name],index) => {
  let path="", last=null, count=0;
  samples.forEach((s,i) => {
   const value = values[index][i];
   if(!Number.isFinite(value)){last=null;return;}
   const join = last !== null && s.time-last <= data.interval_seconds*2.5;
   path += (join?"L":"M")+x(s.time).toFixed(2)+","+y(value).toFixed(2)+" ";
   if(!join) svg.append(svgNode("circle",{cx:x(s.time),cy:y(value),r:2,fill:colors[index]}));
   last=s.time; count++;
  });
  if(count) svg.append(svgNode("path",{d:path,fill:"none",stroke:colors[index],"stroke-width":2,"stroke-linejoin":"round","stroke-dasharray":index ? "6 4" : "none"}));
 });
 if(!finite.length) svg.append(svgNode("text",{x:320,y:100,"text-anchor":"middle",class:"chart-label"},"Waiting for measurements"));
 const legend = document.createElement("div");legend.className="chart-legend";
 config.series.forEach(([name],i) => {
  const item=document.createElement("span");item.className="series-"+i;
  item.textContent=name+": "+number(values[i].at(-1))+" "+config.unit;
  legend.append(item);
 });
 const cursor = document.createElement("p");cursor.className="chart-inspect";
 cursor.textContent="Move over the graph to inspect a sample.";
 svg.addEventListener("pointermove",event=>{
  if(!samples.length)return;
  const bounds=svg.getBoundingClientRect(), aspect=600/210;
  const width=Math.min(bounds.width,bounds.height*aspect), left=bounds.left+(bounds.width-width)/2;
  const time=start+((event.clientX-left)/width*600-58)/524*(end-start);
  const index=samples.reduce((best,s,i)=>Math.abs(s.time-time)<Math.abs(samples[best].time-time)?i:best,0);
  cursor.textContent=new Date(samples[index].time*1000).toLocaleTimeString()+": "+
   config.series.map(([name],i)=>name+" "+number(values[i][index])+" "+config.unit).join(" | ");
 });
 target.append(svg,legend,cursor);
}
function render(){
 if(!data)return;
 const all=data.samples, latest=all.at(-1);
 if(!latest){text("metricsStatus","Waiting for the first server sample.");return;}
 const end=Date.now()/1000, start=end-Number($("metricsRange").value)*60;
 const samples=all.filter(s=>s.time>=start && s.time<=end);
 text("dataIn",mib(counter(latest,"payload_bytes_in")));
 text("dataOut",mib(counter(latest,"payload_bytes_out")));
 text("eventsIn",number(counter(latest,"events_received")));
 text("eventsOut",number(counter(latest,"events_forwarded")));
 for(const role of ["relay","caddy"]){
  const c=latest.containers[role];
  text(role+"Cpu",c.cpu_percent == null ? "Waiting / unavailable" : number(c.cpu_percent)+"%");
  text(role+"Memory",mib(c.memory_bytes));
  text(role+"Limit",!c.available ? "Container accounting unavailable" :
   "Memory limit: "+(c.memory_limit_known === false ? "not reported" : c.memory_limit_bytes == null ? "no container limit" : mib(c.memory_limit_bytes)));
 }
 text("metricsStatus",(end-latest.time>15 ? "Measurements are stale. Last sampled " : "Sampled ")+new Date(latest.time*1000).toLocaleTimeString()+
  " | "+all.length+" samples | Counters since "+new Date(data.started_at*1000).toLocaleString());
 const totals=$("metricTotals");totals.replaceChildren();
 for(const [label,key] of [
  ["Announcements received","announcements_received"],["Announcements forwarded","announcements"],
  ["STOP events received","stops_received"],["Events sent to browser listeners","monitor_events_out"],["Browser events dropped","monitor_events_dropped"],
  ["Receiver-reported speech completions","speech_completed"],["Receiver-reported speech errors","speech_errors"],
  ["Application messages received","messages_in"],["Application messages sent","messages_out"],
  ["Audio frames received","audio_frames_in"],["Audio frames forwarded to receiver","frames_forwarded"],
  ["Receiver audio frames dropped","frames_dropped"],["Browser audio frames dropped","monitor_frames_dropped"]
 ]){
  const dt=document.createElement("dt"),dd=document.createElement("dd");
  dt.textContent=label;dd.textContent=number(counter(latest,key));totals.append(dt,dd);
 }
 charts.forEach(c=>chart(c,samples,start,end));
}
$("metricsRange").addEventListener("change",render);
$("downloadMetrics").addEventListener("click",()=>{
 if(!data)return;
 const blob=new Blob([JSON.stringify(data,null,2)],{type:"application/json"});
 const url=URL.createObjectURL(blob),a=document.createElement("a");
 a.href=url;a.download="RaceVoice-Metrics.json";a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
});
async function poll(){
 try{
  const latest=data?.samples.at(-1);
  const suffix=latest ? "?after="+encodeURIComponent(latest.time) : "";
  const res=await fetch("/admin/api/metrics"+suffix,{cache:"no-store",signal:AbortSignal.timeout(8000)});
  if(!res.ok)throw Error("HTTP "+res.status);
  const incoming=await res.json();
  if(data && incoming.started_at===data.started_at && !incoming.reset){
   const merged=new Map(data.samples.map(s=>[s.time,s]));
   incoming.samples.forEach(s=>merged.set(s.time,s));
   incoming.samples=[...merged.values()].sort((a,b)=>a.time-b.time).slice(-721);
  }
  data=incoming;render();$("downloadMetrics").disabled=false;
 }catch(e){text("metricsStatus","Metrics stale or unavailable: "+e.message+". Retrying automatically.");}
 setTimeout(poll,5000);
}
poll();
})();
