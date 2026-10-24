const fs=require("node:fs"),vm=require("node:vm"),assert=require("node:assert/strict");
const elements={},sockets=[],contexts=[];
function element(id){return elements[id]??=( {value:id==="listenVolume"?"0.6":0,checked:false,textContent:"",handlers:{},addEventListener(event,fn){this.handlers[event]=fn},setAttribute(){}} )}
class Context{
 constructor(){this.state="suspended";this.currentTime=1;this.destination={};this.sources=[];contexts.push(this)}
 async resume(){this.state="running"}
 async close(){this.state="closed"}
 createGain(){return this.gain={gain:{value:1},connect(){}}}
 createBuffer(ch,length,rate){const data=new Float32Array(length);return {rate,data,getChannelData(){return data}}}
 createBufferSource(){const source={connect(){},disconnect(){},start(t){this.when=t},stop(){this.stopped=true}};this.sources.push(source);return source}
}
class Socket{
 constructor(url){this.url=url;sockets.push(this)}
 send(value){this.sent=JSON.parse(value)}
 close(){this.closed=true}
}
const sandbox={document:{getElementById:element},window:{AudioContext:Context,addEventListener(){}},location:{host:"audio.example:8843"},
 fetch:async()=>({ok:true,json:async()=>({ticket:"temporary-ticket"})}),WebSocket:Socket,
 AbortSignal:{timeout(){}},ArrayBuffer,DataView,Float32Array,JSON,Date,Math,Number,Set,Error,
 setTimeout(){return 1},clearTimeout(){},setInterval(){return 1}};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(__dirname+"/../dashboard/monitor.js","utf8"),sandbox);
(async()=>{
 assert.equal(sockets.length,0,"never listen automatically");
 await element("listen").handlers.click();
 const ws=sockets[0];assert.equal(ws.url,"wss://audio.example:8843/admin/monitor");
 ws.onopen();assert.deepEqual(ws.sent,{role:"monitor",key:"temporary-ticket"});
 ws.onmessage({data:JSON.stringify({type:"monitor_ready"})});
 const frame=new ArrayBuffer(1921),view=new DataView(frame);
 view.setUint8(0,1);view.setInt16(1,16384,true);
 ws.onmessage({data:frame});
 const ctx=contexts[0];assert.equal(ctx.sources.length,1);
 assert.equal(ctx.sources[0].buffer.data[0],0.5,"PCM little endian decoding");
 assert.equal(ctx.sources[0].buffer.rate,48000);
 ws.onmessage({data:new ArrayBuffer(3)});
 assert.equal(ctx.sources.length,1,"malformed frames ignored");
 element("listenMute").checked=true;element("listenMute").handlers.change();
 assert.equal(ctx.gain.gain.value,0,"mute controls local playback");
 await element("listen").handlers.click();
 assert.equal(ws.closed,true);assert.equal(ctx.state,"closed");
 assert.equal(ctx.sources[0].stopped,true,"stop clears scheduled audio");
 ws.onmessage({data:frame});assert.equal(ctx.sources.length,1,"stale socket cannot play after stop");
 console.log("PASS: explicit start, ticket auth, PCM decode, invalid frames, mute, stop and stale socket handling");
})().catch(e=>{console.error(e);process.exitCode=1});
