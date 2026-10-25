const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const elements=new Map();
function node(tag="div"){return {tag,children:[],attrs:{},listeners:{},value:"15",textContent:"",
 append(...n){this.children.push(...n)},replaceChildren(){this.children=[]},
 setAttribute(k,v){this.attrs[k]=v},addEventListener(name,fn){this.listeners[name]=fn},click(){this.clicked=true},
 getBoundingClientRect(){return {width:600,height:210,left:0}}}}
const get=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id)};
let current,exported,fail=false,next; const requests=[];
const now=Date.now()/1000;
const sample=(time,index)=>({time,counters:{payload_bytes_in:index*1048576,payload_bytes_out:index*2097152,events_received:index,events_forwarded:index-1},
 rates:index?{payload_bytes_in:1000,payload_bytes_out:2000,events_received:.2,events_forwarded:.2}: {},
 containers:{relay:{available:true,cpu_percent:index?2:null,memory_bytes:10485760,memory_limit_bytes:null},
 caddy:{available:false,cpu_percent:null,memory_bytes:null,memory_limit_bytes:null}}});
current={started_at:now-1200,interval_seconds:5,samples:[sample(now-1000,1),sample(now-5,2),sample(now,3)]};
const context=vm.createContext({document:{getElementById:get,createElement:node,createElementNS:(ns,tag)=>node(tag)},
 Blob,Date,Number,URL:{createObjectURL(blob){exported=blob;return "blob:test"},revokeObjectURL(){}},
 setTimeout(fn,ms){if(ms===5000)next=fn},AbortSignal:{timeout(){}},fetch:async(url)=>{requests.push(url);return {ok:!fail,status:503,json:async()=>current}}});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../dashboard/metrics.js"),"utf8"),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
 await settle();
 assert.equal(get("eventsIn").textContent,"3");
 assert.equal(get("eventsOut").textContent,"2");
 assert.match(get("dataOut").textContent,/6 MiB/);
 assert.match(get("caddyMemory").textContent,/Unavailable/);
 for(const id of ["bandwidthChart","dataChart","eventChart","dropChart","cpuChart","memoryChart"]){
  const svg=get(id).children[0];
  assert.equal(svg.tag,"svg");
  assert(!JSON.stringify(svg).match(/NaN|Infinity/));
 }
 const cpu=get("cpuChart").children[0];
 assert.equal(cpu.children.filter(n=>n.tag==="path").length,1);
 const rateLegend=get("bandwidthChart").children[1];
 assert.match(rateLegend.children[0].textContent,/8 kbps/);
 assert.match(rateLegend.children[1].textContent,/16 kbps/);
 get("metricsRange").value="60";get("metricsRange").listeners.change();
 get("downloadMetrics").listeners.click();
 assert.equal(JSON.parse(await exported.text()).samples.length,3);
 current={...current,samples:[sample(now+5,4)]};await next();await settle();
 assert.match(requests.at(-1),/\?after=/);
 assert.equal(get("eventsIn").textContent,"4");
 get("downloadMetrics").listeners.click();
 assert.equal(JSON.parse(await exported.text()).samples.length,4);
 current={...current,started_at:now,samples:[]};await next();await settle();
 assert.match(get("metricsStatus").textContent,/Waiting/);
 fail=true;await next();await settle();
 assert.match(get("metricsStatus").textContent,/stale or unavailable/);
 console.log("PASS: metrics charts, units, missing accounting, range, export and fetch errors");
})().catch(e=>{console.error(e);process.exitCode=1});
