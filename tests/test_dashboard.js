/* Node DOM mocks check application logic. They do not verify browser layout. */
const fs=require('fs'),vm=require('vm'),assert=require('assert'),path=require('path');
const html=fs.readFileSync(path.join(__dirname,'..','dashboard.html'),'utf8');
const js=html.match(/<script>([\s\S]+?)<\/script>/)[1];
const ctx2d=new Proxy({}, {get:(o,k)=>k in o?o[k]:()=>{},set:(o,k,v)=>(o[k]=v,true)});
const downloads=[];
class Element {
 constructor(tag='div'){
  this.tag=tag;this._text='';this.className='';this.hidden=false;this.disabled=false;this.value='';this.children=[];this.listeners={};this.attributes={};this.clientWidth=320;this.clientHeight=240;this.classList={add(){},remove(){}};
 }
 set textContent(value){this._text=String(value);this.children=[];}
 get textContent(){return this._text;}
 appendChild(e){e.parent=this;this.children.push(e);return e;}
 addEventListener(e,fn){this.listeners[e]=fn;}
 setAttribute(k,v){this.attributes[k]=v;}
 getContext(){return ctx2d;}
 focus(){this.focused=true;}
 scrollIntoView(){this.scrolled=true;}
 click(){if(this.tag==='a')downloads.push({href:this.href,filename:this.download});if(this.listeners.click)return this.listeners.click({preventDefault(){}});}
 remove(){if(this.parent)this.parent.children=this.parent.children.filter(child=>child!==this);}
}
const elems={};
for(const match of html.matchAll(/<([a-z]+)\b[^>]*\bid="([^"]+)"[^>]*>/g)){
 const el=new Element(match[1]);el.hidden=/\shidden(?:\s|>)/.test(match[0]);el.disabled=/\sdisabled(?:\s|>)/.test(match[0]);el.value=(match[0].match(/\bvalue="([^"]*)"/)||[])[1]||'';elems[match[2]]=el;
}
elems.adminDuration.value='30';
let now=100,failed=false,notifications=[],beeps=0;
let current={boot_id:'bootA',current:{raw:35000,voltage:1.762,percent:53.4,change:5000,rise_per_second:5000,state:'danger',status:'위험',alarm_level:'danger',time:12,sample_id:12,source:'simulation',warmup_remaining:0,health:'ok',health_message:'입력 확인 중',rapid_rise:false,pending:''},samples:[{id:1,time:1,raw:10000},{id:2,time:2,raw:null},{id:3,time:3,raw:35000}],events:[{id:1,boot_id:'bootA',time:12,raw:35000,state:'danger',status:'위험',type:'state_change',message:'<img src=x onerror=alert(1)>',source:'simulation'}],stats:{average:22500,maximum:35000,minimum:10000,count:2,total_count:12},settings:{warning_level:18000,danger_level:30000,hysteresis:2000,enter_count:3,exit_count:5,warmup_seconds:0,history_size:60,event_size:30,sample_interval_ms:1000,rapid_rise_per_second:8000,rapid_rise_count:2},device:{wifi_connected:true,ip:'192.168.1.20',uptime_ms:12000,last_sample_age_ms:10,simulation:true,firmware_version:'2.0'}};
class NotificationMock{static permission='granted';constructor(title,data){notifications.push({title,data});}static async requestPermission(){return 'granted';}}
class AudioMock{constructor(){this.state='running';this.currentTime=0;this.destination={};}createOscillator(){return{frequency:{value:0},connect(){},disconnect(){},start(){beeps++;},stop(){}};}createGain(){return{gain:{setValueAtTime(){},exponentialRampToValueAtTime(){}},connect(){},disconnect(){}};}}
const intervals=[],timeouts=[],windowEvents={},requests=[],blobs=[],revoked=[];
const inactive=reason=>({enabled:true,active:false,id:null,kind:null,mode:null,label:'관리자 테스트 없음',phase:null,remaining_seconds:0,stop_reason:reason||''});
const baseLive={raw:12000,status:'정상',state:'normal',alarm_level:'normal',source:'sensor',health:'ok',pending:''};
let admin={token:null,expiresAt:0,test:inactive(),hasVirtual:false,audit:[{id:1,time_ms:1234,action:'login',message:'<img src=x onerror=alert(1)>',test_id:null}],live:{...baseLive}};
let loginFailure=null,statusFailure=null,startFailure=null,holdStatus=false,releaseStatus=null,holdStart=false,releaseStart=null;
const response=(status,data)=>({ok:status>=200&&status<300,status,json:async()=>structuredClone(data),blob:async()=>new Blob(['source,raw\nsimulation,30000\n'],{type:'text/csv'})});
function privateState(){return{authenticated:true,expires_in:Math.max(0,Math.ceil((admin.expiresAt-now)/1000)),test:{...admin.test},audit:admin.audit,limits:{max_seconds:120,lease_seconds:12},device:{ip:'192.168.1.20',uptime_ms:now,free_heap:100000,base_simulation:false},live:{...admin.live}};}
function start(spec){admin.test={enabled:true,active:true,id:'test-'+requests.length,kind:spec.kind,mode:spec.mode||null,label:'테스트 '+spec.kind,phase:spec.mode||'normal',remaining_seconds:spec.duration,stop_reason:''};if(spec.kind!=='led')admin.hasVirtual=true;return response(200,privateState());}
async function fakeFetch(url,options={}){
 requests.push({url,options});
 if(url==='/data'){if(failed)throw new Error('network');return response(200,current);}
 assert(url.startsWith('/admin/'),'All requests must use a relative same-origin path');
 if(url==='/admin/login'){
  if(loginFailure)return response(loginFailure.status,{error:loginFailure.message,retry_after:loginFailure.retry_after});
  admin.token='node-test-token-'+requests.length;admin.expiresAt=now+600000;admin.test=inactive();return response(200,{token:admin.token,expires_in:600});
 }
 if(!admin.token||options.headers.Authorization!=='Bearer '+admin.token||now>=admin.expiresAt)return response(401,{error:'관리자 세션이 만료되었습니다.'});
 if(url==='/admin/status'){
  if(statusFailure)return response(statusFailure.status,{error:statusFailure.message,retry_after:statusFailure.retry_after});
  if(holdStatus)return new Promise(resolve=>{releaseStatus=()=>resolve(response(200,privateState()));});
  return response(200,privateState());
 }
 if(url==='/admin/test'){
  if(startFailure)return response(startFailure.status,{error:startFailure.message,retry_after:startFailure.retry_after});
  const spec=JSON.parse(options.body);
  if(holdStart)return new Promise(resolve=>{releaseStart=()=>resolve(start(spec));});
  return start(spec);
 }
 if(url==='/admin/stop'){admin.test=inactive('관리자 중지');return response(200,privateState());}
 if(url==='/admin/logout'){admin.token=null;admin.test=inactive('관리자 로그아웃');return response(200,{authenticated:false,test:admin.test});}
 if(url==='/admin/test-history.csv'&&!admin.hasVirtual)return response(404,{error:'아직 가상 시험 기록이 없습니다.'});
 if(url==='/admin/test-history.csv'||url==='/admin/audit.csv')return response(200,{});
 throw new Error('Unexpected URL '+url);
}
const context={document:{getElementById:id=>elems[id],createElement:tag=>new Element(tag),body:new Element('body')},window:{devicePixelRatio:1,isSecureContext:false,addEventListener:(name,fn)=>{windowEvents[name]=fn;},Notification:NotificationMock,AudioContext:AudioMock},performance:{now:()=>now},Notification:NotificationMock,AbortController,Blob,TypeError,URL:{createObjectURL(blob){blobs.push(blob);return'blob:node-test/'+blobs.length;},revokeObjectURL(url){revoked.push(url);}},setTimeout:(fn,delay)=>(timeouts.push({fn,delay,active:true}),timeouts.length),clearTimeout:id=>{if(timeouts[id-1])timeouts[id-1].active=false;},setInterval:fn=>(intervals.push(fn),intervals.length),fetch:fakeFetch,console};
vm.createContext(context);
const flush=()=>new Promise(resolve=>setImmediate(resolve));
const run=code=>vm.runInContext(code,context);
const click=id=>elems[id].listeners.click({preventDefault(){}});
const submit=()=>elems.adminLoginForm.listeners.submit({preventDefault(){}});
const countRequests=url=>requests.filter(r=>r.url===url).length;
const lastRequest=url=>requests.filter(r=>r.url===url).at(-1);
const update=async()=>{current.current.sample_id++;await run('update()');};
const login=async()=>{elems.adminPassword.value='dummy-input-for-node-test';await submit();};
(async()=>{
 run(js);await flush();
 assert.equal(elems.status.textContent,'위험');assert.equal(elems.simulation.hidden,false);assert.equal(elems.notifyButton.disabled,true);assert(elems.notifyStatus.textContent.includes('HTTP'));
 assert.equal(elems.count.textContent,'2');assert.equal(elems.total.textContent,'부팅 후 총 측정: 12회 (실패 포함)');
 assert(elems.events.children[0].children[3].textContent.includes('<img src=x'));assert(elems.events.children[0].children.every(e=>e.children.length===0));
 console.log('PASS initial danger, simulation, HTTP notification limitation, counts, escaped event text');
 await click('soundButton');assert.equal(elems.soundButton.attributes['aria-pressed'],'true');assert.equal(beeps,1);run('maybeSound()');assert.equal(beeps,1);
 now+=3100;intervals[0]();assert.equal(beeps,2);await click('soundButton');assert.equal(elems.soundButton.attributes['aria-pressed'],'false');
 console.log('PASS explicit audio unlock, 3-second rate limit, sound disable');
 now+=2000;intervals[0]();assert.equal(elems.status.textContent,'측정 연결 확인 필요');assert(elems.rawNote.textContent.includes('오래된 값'));
 await run('update()');assert.equal(elems.status.textContent,'측정 연결 확인 필요');await update();assert.equal(elems.status.textContent,'위험');
 current.device.last_sample_age_ms=5000;await update();assert.equal(elems.status.textContent,'측정 연결 확인 필요');
 current.device.last_sample_age_ms=10;await update();failed=true;await run('update()');assert.equal(elems.status.textContent,'측정 연결 확인 필요');assert.equal(elems.raw.textContent,'35,000');assert.equal(run('fresh'),false);
 console.log('PASS stale sample ID, stale device age, disconnect preserves and labels last values');
 failed=false;context.window.isSecureContext=true;elems.notifyButton.disabled=false;run('initNotifications()');await update();assert.equal(notifications.length,1);await update();assert.equal(notifications.length,1);
 current.boot_id='bootB';current.current.sample_id=0;await update();assert.equal(notifications.length,2);current.current.state='normal';await update();assert.equal(notifications.length,2);current.current.state='warning';await update();assert.equal(notifications.length,3);
 console.log('PASS first/reconnect alert, duplicate suppression, reboot reset, repeat warning transition');
 current.current.state='warmup';current.current.warmup_remaining=120;await update();assert(elems.bannerDetail.textContent.includes('120초'));current.current.state='danger';await update();assert.equal(elems.status.textContent,'위험');assert(elems.bannerDetail.textContent.includes('120초'));
 current.current.state='fault';current.current.raw=null;await update();assert.equal(elems.status.textContent,'센서 점검 필요');assert.equal(elems.raw.textContent,'—');
 console.log('PASS warmup countdown, danger during warmup, sensor read fault');

 assert.equal(elems.adminPanel.hidden,true);assert.equal(elems.adminControls.disabled,true);assert.equal(elems.adminStop.disabled,true);
 await click('adminToggle');assert.equal(elems.adminPanel.hidden,false);assert.equal(elems.adminToggle.attributes['aria-expanded'],'true');assert(elems.adminPassword.focused);
 await click('testLedGreen');assert.equal(countRequests('/admin/test'),0);
 loginFailure={status:401,message:'비밀번호가 올바르지 않습니다.'};await login();assert.equal(elems.adminPassword.value,'');assert.equal(run('adminToken'),null);assert(elems.adminMessage.textContent.includes('올바르지'));
 loginFailure={status:429,message:'잠시 후 다시 시도하세요.',retry_after:3};await login();assert(elems.adminLoginButton.disabled);let n=countRequests('/admin/login');await login();assert.equal(countRequests('/admin/login'),n);now+=3100;intervals[0]();assert.equal(elems.adminLoginButton.disabled,false);
 loginFailure=null;await login();assert.equal(elems.adminPassword.value,'');assert.equal(elems.adminLoginForm.hidden,true);assert.equal(elems.adminControls.disabled,false);assert.equal(run('adminToken'),admin.token);
 const loginRequest=lastRequest('/admin/login');assert.equal(loginRequest.options.headers['Content-Type'],'application/json');assert(!loginRequest.options.headers.Authorization);assert.equal(JSON.parse(loginRequest.options.body).password,'dummy-input-for-node-test');
 assert(!/localStorage|sessionStorage|document\.cookie/.test(js));assert(!js.includes('dummy-input-for-node-test'));assert(!js.includes('node-test-token'));
 assert(elems.adminAudit.children[0].children[2].textContent.includes('<img src=x'));assert.equal(elems.adminAudit.children[0].children[2].children.length,0);
 assert.equal(lastRequest('/admin/status').options.headers.Authorization,'Bearer '+admin.token);
 console.log('PASS login gating, cleared password, 401/429 handling, memory-only token, authenticated status, escaped audit');

 holdStatus=true;const beforePoll=countRequests('/admin/status'),deadline=run('adminExpiresAt');const firstPoll=run('pollAdmin()'),secondPoll=run('pollAdmin()');assert.equal(countRequests('/admin/status'),beforePoll+1);releaseStatus();await Promise.all([firstPoll,secondPoll]);holdStatus=false;
 assert(run('adminExpiresAt')<=deadline);assert(timeouts.some(t=>t.active&&t.delay===3000));
 statusFailure={status:503,message:'장치가 처리 중입니다.'};await run('pollAdmin()');assert(elems.adminControls.disabled);assert(!elems.adminStop.disabled);assert(elems.adminMessage.textContent.includes('처리 중'));statusFailure=null;await run('pollAdmin()');assert(!elems.adminControls.disabled);
 console.log('PASS non-overlapping heartbeat, 3-second reschedule, absolute expiry, busy response recovery');

 await click('adminTestCsv');assert(elems.adminMessage.textContent.includes('가상 시험 기록'));assert.equal(downloads.length,0);
 for(const [id,mode,duration] of [['testLedGreen','green',5],['testLedYellow','yellow',5],['testLedRed','red',5],['testLedAll','all',3],['testLedSequence','sequence',9]]){
  await click(id);assert.deepEqual(JSON.parse(lastRequest('/admin/test').options.body),{kind:'led',mode,duration});assert.equal(elems.adminTestBanner.hidden,false);assert(elems.adminTestDescription.textContent.includes('LED만 시험'));assert(elems.adminTestDescription.textContent.includes('실제 측정'));
 }
 for(const [id,mode] of [['testSensorNormal','normal'],['testSensorWarning','warning'],['testSensorDanger','danger'],['testSensorFault','fault']]){
  await click(id);assert.deepEqual(JSON.parse(lastRequest('/admin/test').options.body),{kind:'sensor',mode,duration:30});assert(elems.adminTestDescription.textContent.includes('모의 센서 시험'));assert(elems.adminLiveValue.textContent.includes('12,000'));
 }
 n=countRequests('/admin/test');for(const input of ['','-1','1.5','65536','1e3']){elems.adminRaw.value=input;await click('testSensorRaw');assert.equal(countRequests('/admin/test'),n);}
 for(const raw of [0,65535]){elems.adminRaw.value=String(raw);await click('testSensorRaw');assert.equal(JSON.parse(lastRequest('/admin/test').options.body).raw,raw);}
 elems.adminDuration.value='120';await click('testSensorWarning');assert.equal(JSON.parse(lastRequest('/admin/test').options.body).duration,120);elems.adminDuration.value='30';
 await click('testScenario');assert.deepEqual(JSON.parse(lastRequest('/admin/test').options.body),{kind:'sequence',duration:60});
 startFailure={status:409,message:'실제 센서 경보가 있어 시험을 시작하지 않습니다.'};await click('testSensorDanger');assert(elems.adminMessage.textContent.includes('시작하지 않습니다'));assert(!elems.adminMessage.textContent.includes('시험이 시작되었습니다'));startFailure=null;
 console.log('PASS fixed LED durations, all sensor modes, raw bounds, selected duration, scenario, rejected start is not success');

 await click('adminTestCsv');await click('adminAuditCsv');assert.equal(downloads.length,2);assert.equal(downloads[0].filename,'gas_admin_test_history.csv');assert.equal(downloads[1].filename,'gas_admin_audit.csv');assert(downloads.every(d=>d.href.startsWith('blob:')));
 assert.equal(lastRequest('/admin/test-history.csv').options.headers.Authorization,'Bearer '+admin.token);assert.equal(lastRequest('/admin/audit.csv').options.headers.Authorization,'Bearer '+admin.token);
 for(const request of requests.filter(r=>r.url.startsWith('/admin/'))){assert.equal(request.options.mode,'same-origin');assert.equal(request.options.redirect,'error');assert(!request.url.includes(admin.token));if(request.options.method==='POST')assert.equal(request.options.headers['Content-Type'],'application/json');}
 console.log('PASS protected CSV downloads via authenticated fetch and temporary Blob URL');

 await click('adminStop');assert.equal(admin.test.active,false);assert(elems.adminMessage.textContent.includes('시험 종료를 확인'));assert.deepEqual(JSON.parse(lastRequest('/admin/stop').options.body),{});
 holdStart=true;const starting=click('testLedGreen');await flush();assert(!elems.adminStop.disabled);const stopBefore=countRequests('/admin/stop');const stopping=click('adminStop');await flush();assert.equal(countRequests('/admin/stop'),stopBefore+1);releaseStart();await Promise.all([starting,stopping]);holdStart=false;
 assert.equal(countRequests('/admin/stop'),stopBefore+2);assert.equal(admin.test.active,false);assert(elems.adminMessage.textContent.includes('시험 종료를 확인'));
 console.log('PASS immediate stop, stop remains available during start, late start cannot overwrite final stop');

 current.admin={enabled:true,active:true,id:'public-test',kind:'sensor',mode:'danger',label:'모의 위험',phase:'danger',remaining_seconds:30,stop_reason:''};current.live={...baseLive};current.device.base_simulation=false;current.device.simulation=true;current.current.source='simulation';current.current.state='danger';current.current.raw=45000;current.current.warmup_remaining=0;
 await update();assert.equal(elems.adminTestBanner.hidden,false);assert(elems.adminLiveValue.textContent.includes('12,000'));assert(elems.adminTestDescription.textContent.includes('시험용 값'));assert(elems.total.textContent.startsWith('이번 모의 시험'));assert.equal(elems.simulation.hidden,true);
 current.admin=inactive('실제 센서 주의·위험 경보로 테스트 종료');current.live={...baseLive,raw:32000,state:'danger',status:'위험',alarm_level:'danger'};current.device.simulation=false;current.current.source='sensor';current.current.raw=32000;current.boot_id='base-restored';await update();assert(elems.adminStopReason.textContent.includes('실제 센서'));assert.equal(elems.status.textContent,'위험');assert.equal(elems.adminTestBanner.hidden,false);
 current.admin={enabled:true,active:true,id:'led-base-demo',kind:'led',mode:'green',label:'초록 시험',phase:'green',remaining_seconds:3,stop_reason:''};current.live={...baseLive,source:'simulation'};current.device.base_simulation=true;current.device.simulation=true;await update();assert(elems.adminTestDescription.textContent.includes('기본 모의'));assert.equal(elems.simulation.hidden,false);assert(elems.simulation.textContent.includes('끝나도'));
 now+=5000;run('updateTestBanner()');assert(elems.adminTestCountdown.textContent.includes('현재 상태 확인 필요'));assert(!elems.adminTestCountdown.textContent.includes('종료 확인'));
 console.log('PASS public test banner, underlying sensor separation, real-alarm preemption message, original demo mode, stale countdown');

 const logoutToken=admin.token;await click('adminLogout');assert.equal(run('adminToken'),null);assert.equal(elems.adminControls.disabled,true);assert.equal(lastRequest('/admin/logout').options.headers.Authorization,'Bearer '+logoutToken);assert(elems.adminMessage.textContent.includes('로그아웃했습니다'));
 await login();admin.token=null;await run('pollAdmin()');assert.equal(run('adminToken'),null);assert(elems.adminControls.disabled);assert(elems.adminMessage.textContent.includes('세션이 종료'));
 await login();await click('testLedGreen');const pageToken=admin.token;windowEvents.pagehide();await flush();assert.equal(run('adminToken'),null);assert(elems.adminControls.disabled);assert.equal(lastRequest('/admin/stop').options.keepalive,true);assert.equal(lastRequest('/admin/stop').options.headers.Authorization,'Bearer '+pageToken);windowEvents.pageshow();
 await login();now+=600001;intervals[0]();assert.equal(run('adminToken'),null);assert(elems.adminMessage.textContent.includes('세션 시간이 끝'));
 console.log('PASS logout, expired/invalid token clearing, pagehide best-effort stop, client session deadline');
 console.log('ALL dashboard DOM-mock checks passed. Actual browser rendering and physical LEDs are not verified by these tests.');
})().catch(error=>{console.error(error);process.exit(1);});
