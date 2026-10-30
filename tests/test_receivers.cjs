const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const elements=new Map();
function node(){return {children:[],value:"all",textContent:"",type:"password",append(...n){this.children.push(...n)},replaceChildren(){this.children=[]},addEventListener(){},setAttribute(){}}}
function get(id){if(!elements.has(id))elements.set(id,node());return elements.get(id)}
let relay={uptime_seconds:10,sender_connected:true,receiver_connected:true,receiver_count:3,receiver_enabled_count:2,receiver_enabled:true,connected_at:{sender:1},stats:{audio_bytes_out:0},connections:{sender:1,receiver:4},events:[],client_logs:[]};
const context=vm.createContext({document:{getElementById:get,createElement:node,addEventListener(){}},Date,Number,Blob,URL,AbortSignal,setTimeout(){},clearTimeout(){},fetch:async()=>({ok:true,json:async()=>({relay,certificate:{},hostname:"audio.example",port:8843})})});
vm.runInContext(fs.readFileSync(path.join(__dirname,"../dashboard/app.js"),"utf8"),context);
(async()=>{
 await new Promise(setImmediate);
 assert.equal(get("receiver").textContent,"3 connected");
 assert.equal(get("enabled").textContent,"2 of 3 enabled");
 relay={...relay,receiver_count:0,receiver_enabled_count:0,receiver_connected:false,receiver_enabled:false};
 await vm.runInContext("refresh()",context);
 assert.equal(get("receiver").textContent,"0 connected");
 assert.equal(get("enabled").textContent,"0 of 0 enabled");
 delete relay.receiver_count;delete relay.receiver_enabled_count;
 relay.receiver_connected=true;relay.receiver_enabled=true;
 await vm.runInContext("refresh()",context);
 assert.equal(get("receiver").textContent,"1 connected");
 assert.equal(get("enabled").textContent,"1 of 1 enabled");
 console.log("PASS: portal receiver counts, enable counts, disconnect and legacy fallback");
})().catch(e=>{console.error(e);process.exitCode=1});
