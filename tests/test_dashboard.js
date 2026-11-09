const fs=require('fs'),vm=require('vm'),assert=require('assert');
const path=require('path');
const html=fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
const js=html.match(/<script>([\s\S]+?)<\/script>/)[1];
const ctx2d=new Proxy({}, {get:(o,k)=> k in o ? o[k] : ()=>{},set:(o,k,v)=>(o[k]=v,true)});
class Element {
 constructor(){this.textContent='';this.className='';this.hidden=false;this.disabled=false;this.children=[];this.listeners={};this.attributes={};this.clientWidth=320;this.clientHeight=240;this.classList={add:()=>{},remove:()=>{}};}
 appendChild(e){this.children.push(e);return e;}
 addEventListener(e,fn){this.listeners[e]=fn;}
 setAttribute(k,v){this.attributes[k]=v;}
 getContext(){return ctx2d;}
}
const elems=Object.fromEntries([...html.matchAll(/\bid="([^"]+)"/g)].map(m=>[m[1],new Element()]));
let now=100,failed=false,notifications=[],beeps=0;
let current={boot_id:'bootA',current:{raw:35000,voltage:1.762,percent:53.4,change:5000,rise_per_second:5000,state:'danger',status:'위험',alarm_level:'danger',time:12,sample_id:12,source:'simulation',warmup_remaining:0,health:'ok',health_message:'입력 확인 중',rapid_rise:false,pending:''},samples:[{id:1,time:1,raw:10000},{id:2,time:2,raw:null},{id:3,time:3,raw:35000}],events:[{id:1,boot_id:'bootA',time:12,raw:35000,state:'danger',status:'위험',type:'state_change',message:'<img src=x onerror=alert(1)>',source:'simulation'}],stats:{average:22500,maximum:35000,minimum:10000,count:2,total_count:12},settings:{warning_level:18000,danger_level:30000,hysteresis:2000,enter_count:3,exit_count:5,warmup_seconds:0,history_size:60,event_size:30,sample_interval_ms:1000,rapid_rise_per_second:8000,rapid_rise_count:2},device:{wifi_connected:true,ip:'192.168.1.20',uptime_ms:12000,last_sample_age_ms:10,simulation:true,firmware_version:'2.0'}};
class NotificationMock{static permission='granted';constructor(title,data){notifications.push({title,data});}static async requestPermission(){return 'granted';}}
class AudioMock{constructor(){this.state='running';this.currentTime=0;this.destination={};}createOscillator(){return{frequency:{value:0},connect(){},disconnect(){},start(){beeps++;},stop(){}};}createGain(){return{gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){},disconnect(){}};}}
const intervals=[],timeouts=[];
const context={document:{getElementById:id=>elems[id],createElement:()=>new Element()},window:{devicePixelRatio:1,isSecureContext:false,addEventListener(){},Notification:NotificationMock,AudioContext:AudioMock},performance:{now:()=>now},Notification:NotificationMock,AbortController,setTimeout:fn=>(timeouts.push(fn),timeouts.length),clearTimeout(){},setInterval:fn=>(intervals.push(fn),intervals.length),fetch:async()=>{if(failed)throw Error('network');return{ok:true,json:async()=>structuredClone(current)};},console};
vm.createContext(context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const run=code=>vm.runInContext(code,context);
(async()=>{
 run(js);await flush();
 assert.equal(elems.status.textContent,'위험');
 assert.equal(elems.simulation.hidden,false);
 assert.equal(elems.notifyButton.disabled,true);
 assert(elems.notifyStatus.textContent.includes('HTTP'));
 assert.equal(elems.count.textContent,'2');
 assert.equal(elems.total.textContent,'부팅 후 총 측정: 12회 (실패 포함)');
 assert(elems.events.children[0].children[3].textContent.includes('<img src=x'));
 assert(elems.events.children[0].children.every(e=>e.children.length===0));
 console.log('PASS initial danger, simulation, HTTP notification limitation, counts, escaped event text');
 await elems.soundButton.listeners.click();assert.equal(elems.soundButton.attributes['aria-pressed'],'true');assert.equal(beeps,1);
 run('maybeSound()');assert.equal(beeps,1);
 now+=3100;intervals[0]();assert.equal(beeps,2);
 await elems.soundButton.listeners.click();assert.equal(elems.soundButton.attributes['aria-pressed'],'false');
 console.log('PASS explicit audio unlock, 3-second rate limit, sound disable');
 now+=2000;intervals[0]();assert.equal(elems.status.textContent,'측정 연결 확인 필요');assert(elems.rawNote.textContent.includes('오래된 값'));
 await run('update()');assert.equal(elems.status.textContent,'측정 연결 확인 필요');
 current.current.sample_id++;await run('update()');assert.equal(elems.status.textContent,'위험');
 current.device.last_sample_age_ms=5000;current.current.sample_id++;await run('update()');assert.equal(elems.status.textContent,'측정 연결 확인 필요');
 current.device.last_sample_age_ms=10;current.current.sample_id++;await run('update()');
 failed=true;await run('update()');assert.equal(elems.status.textContent,'측정 연결 확인 필요');assert.equal(elems.raw.textContent,'35,000');assert.equal(run('fresh'),false);
 console.log('PASS stale sample ID, stale device age, disconnect preserves and labels last values');
 failed=false;context.window.isSecureContext=true;elems.notifyButton.disabled=false;run('initNotifications()');current.current.sample_id++;await run('update()');assert.equal(notifications.length,1);
 current.current.sample_id++;await run('update()');assert.equal(notifications.length,1);
 current.boot_id='bootB';current.current.sample_id=1;await run('update()');assert.equal(notifications.length,2);
 current.current.state='normal';current.current.sample_id++;await run('update()');assert.equal(notifications.length,2);
 current.current.state='warning';current.current.sample_id++;await run('update()');assert.equal(notifications.length,3);
 console.log('PASS first/reconnect alert, duplicate suppression, reboot reset, repeat warning transition');
 current.current.state='warmup';current.current.warmup_remaining=120;current.current.sample_id++;await run('update()');assert(elems.bannerDetail.textContent.includes('120초'));
 current.current.state='danger';current.current.sample_id++;await run('update()');assert.equal(elems.status.textContent,'위험');assert(elems.bannerDetail.textContent.includes('120초'));
 current.current.state='fault';current.current.raw=null;current.current.sample_id++;await run('update()');assert.equal(elems.status.textContent,'센서 점검 필요');assert.equal(elems.raw.textContent,'—');
 console.log('PASS warmup countdown, danger during warmup, sensor read fault');
 console.log('ALL dashboard DOM-mock logic checks passed. Actual layout/browser rendering remains untested (no Chromium binary).');
})().catch(e=>{console.error(e);process.exit(1);});
