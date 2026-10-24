const assert=require("node:assert/strict"),fs=require("node:fs"),vm=require("node:vm"),path=require("node:path");
const elements=new Map();
function node(){return {children:[],listeners:{},value:"all",textContent:"",append(...n){this.children.push(...n)},replaceChildren(){this.children=[]},addEventListener(name,fn){this.listeners[name]=fn},click(){this.clicked=true}}}
const get=id=>{if(!elements.has(id))elements.set(id,node());return elements.get(id)};
let exported=null;
const context=vm.createContext({document:{getElementById:get,createElement:node},Blob,Date,Number,URL:{createObjectURL(blob){exported=blob;return "blob:test"},revokeObjectURL(){}},setTimeout(){}});
const source=fs.readFileSync(path.join(__dirname,"../dashboard/app.js"),"utf8");
vm.runInContext(source.slice(0,source.indexOf("function row(")),context);
vm.runInContext('clientLogs=[{role:"sender",event:"command_sent",id:"abc",characters:42,time:1},{role:"receiver",event:"speech_completed",id:"abc",seconds:8.2,time:2,detail:"completed",version:"0.3.6"}];renderClientLogs()',context);
assert.equal(get("clientLogs").children.length,2);
assert.match(get("clientLogs").children[1].children[1].textContent,/8.20 seconds/);
get("clientLogRole").value="receiver";get("clientLogRole").listeners.change();
assert.equal(get("clientLogs").children.length,1);
assert.match(get("clientLogs").children[0].children[0].textContent,/Receiver: Speech completed/);
get("downloadClientLogs").listeners.click();
exported.text().then(raw=>{const result=JSON.parse(raw);assert.equal(result.events.length,2);assert.equal(result.events[0].id,"abc");console.log("PASS: portal logs render, role filter and JSON download");});
