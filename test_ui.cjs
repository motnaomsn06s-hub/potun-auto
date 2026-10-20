// UI regression tests for the renewed shell (今日／過去／成績／その他／レース) without a browser.
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
function extract(a,b){const i=html.indexOf(a);assert(i>=0,'missing '+a);const j=html.indexOf(b,i);assert(j>i,'missing '+b);return html.slice(i,j);}
const el=()=>({innerHTML:'',textContent:'',hidden:false,style:{display:'block'},disabled:false,classList:{add(){},remove(){}},contains:()=>true,setAttribute(){},addEventListener(){}});
(async()=>{
 const elements={nsView:el(),msg:el(),detail:el(),nsCancel:el(),learn:el(),roi:el(),nsHealth:el(),nsRecord:el(),nsMore:el(),nsRaceBar:el(),headerRefresh:el()};
 const alerts=[];const store={};
 const ctx={fetch:async()=>({ok:false,status:503,json:async()=>({ok:false,error:'接続失敗'})}),$:id=>elements[id]||null,
  esc:s=>String(s??''),confirm:()=>true,alert:m=>alerts.push(m),cur:'raceA',location:{hash:'#/race/raceA'},
  sessionStorage:{getItem:k=>store[k]??null,setItem:(k,v)=>{store[k]=v}},encodeURIComponent,decodeURIComponent,Date,Math,
  document:{querySelectorAll:()=>[],addEventListener(){},hidden:false},window:{scrollTo(){},addEventListener(){}},
  stopFlow(){},updateClock(){},show:async()=>{},setInterval(){},renderStatus(){throw Error('stale response rendered')}};
 vm.createContext(ctx);
 vm.runInContext(extract('const NS_WINDOWS=','// ---- boot ----')+';this.NS=NS;',ctx);

 // 1. A failed day request shows the error and never pretends the day is empty.
 ctx.NS.date='2026-10-20';await ctx.nsShowDay('2026-10-20');
 assert(elements.nsView.innerHTML.includes('接続失敗'),elements.nsView.innerHTML);
 assert(!elements.nsView.innerHTML.includes('まだありません'));

 // 2. Network failure on cancel ("Load failed" on iPhone) is reported; nothing is cleared.
 ctx.fetch=async()=>{throw new TypeError('Load failed')};
 await ctx.cancelR('raceA');
 assert.equal(ctx.cur,'raceA');assert.equal(elements.detail.style.display,'block');assert.equal(ctx.location.hash,'#/race/raceA');
 assert(elements.msg.textContent.includes('取り消せません'));assert(alerts.some(a=>a.includes('Load failed')));

 // 3. A day is grouped by venue with tabs; lamps, picks, results and cancellations render.
 const now=new Date('2026-10-20T16:50:00+09:00');
 const R=(k,v,n,t,o)=>Object.assign({race_key:k,baba_name:v,race:n,start_iso:'2026-10-20T'+t+':00+09:00',status:'reserved',slots:[],picks:[],result:null},o||{});
 const races=[R('a1','浦和',5,'14:20',{status:'complete',slots:[15,10,5,4,3],picks:[3,2,7],result:{order:[3,2,9],places:[1,2,2]}}),
   R('a2','浦和',9,'16:35',{slots:[15,5,4,3]}),R('a3','浦和',10,'17:10',{race_name:'秋風特別',course:'左1400m',field_size:11}),
   R('b1','盛岡',9,'16:25',{status:'cancelled'}),R('b2','盛岡',11,'17:35'),R('c1','門別',6,'17:30')];
 ctx.NS.venue=null;
 let out=ctx.nsDayHTML('2026-10-20',races,now);
 assert(out.includes('data-venue="門別"')&&out.includes('data-venue="盛岡"')&&out.includes('data-venue="浦和"'));
 assert(out.indexOf('data-venue="門別"')<out.indexOf('data-venue="盛岡"')&&out.indexOf('data-venue="盛岡"')<out.indexOf('data-venue="浦和"'),'north-to-south venue order');
 assert(out.includes('ns-board'),'timeline board');assert(out.includes('class="ns-pt next"'),'next race marked');
 assert.equal(ctx.NS.venue,'浦和','default tab = venue of next race');
 assert(out.includes('秋風特別')&&out.includes('左1400m　11頭'));
 assert(out.includes('門別<small>次 17:30</small>')&&out.includes('浦和<small>次 17:10</small>'),'tabs show next post time');
 assert(out.includes('class="ns-lane-name" data-venue="盛岡"'),'board lane switches venue');
 assert(out.includes('<i class="miss">10</i>'),'missed 10-minute slot shown');
 assert(out.includes('<em class="hit">3</em><em class="hit">2</em><em class="">7</em>'),'FINAL picks marked against 2着同着 result');
 assert(out.includes('3-2-9'));
 ctx.NS.venue='盛岡';out=ctx.nsDayHTML('2026-10-20',races,now);
 assert(out.includes('ns-race off'),'cancelled race dimmed');assert(!out.includes('class="ns-race" data-race="a2"'),'other venue rows hidden');assert(out.includes('class="ns-race" data-race="b2"'));

 // 4. Past days list excludes the removed 2026-10-07 and links today to the home view.
 ctx.fetch=async()=>({ok:true,status:200,json:async()=>({ok:true,days:[{date:ctx.nsToday(),races:48,complete:20,venues:'門別・浦和'},{date:'2026-10-07',races:9,complete:9,venues:'大井'},{date:'2026-10-05',races:10,complete:10,venues:'川崎'}]})});
 await ctx.nsShowDays();
 assert(!elements.nsView.innerHTML.includes('10月7日'));assert(elements.nsView.innerHTML.includes('#/past/2026-10-05'));assert(elements.nsView.innerHTML.includes('href="#/"'));

 // 5. Successful cancel requires the server to confirm status=cancelled, then returns to the list.
 ctx.NS.back='#/';ctx.NS.date=null;
 ctx.fetch=async u=>u.startsWith('/api/cancel')?{ok:true,status:200,json:async()=>({ok:true,removed:true,status:'cancelled'})}:{ok:true,status:200,json:async()=>({ok:true,races:[]})};
 await ctx.cancelR('raceA');
 assert.equal(ctx.cur,null);assert.equal(ctx.location.hash,'#/');assert(elements.msg.textContent.includes('やめました'));
 ctx.cur='raceA';ctx.fetch=async()=>({ok:true,status:200,json:async()=>({ok:false,error:'予約が見つかりません'})});
 await ctx.cancelR('raceA');assert.equal(ctx.cur,'raceA');

 // 6. Race-page labels are shown in Japanese; logic keys are untouched.
 const J=ctx.nsJaText;
 assert.equal(J('10分前CANDIDATE待ち'),'10分前候補待ち');assert.equal(J('S PRIME'),'S 最強');assert.equal(J('FINAL順'),'最終順');
 assert.equal(J('LATE UPDATE'),'直前更新');assert.equal(J('SIGNAL LEVEL'),'シグナルの強さ');assert.equal(J('3MIN FINAL'),'3分前 最終');
 assert.equal(J('RESULT GUARD COLLECTING'),'結果による抑制 集計中');assert.equal(J('GAP · ISOLATION · POTUN'),'断層 · 孤立 · ポツン');
 assert.equal(J('ODDS DNA'),'オッズDNA');assert.equal(J('馬番 7'),'馬番 7');
 // 7. Stale detail responses are not rendered after switching race.
 vm.runInContext(extract('async function show(k,silent=false)','document.addEventListener'),ctx);
 let resolveFetch;ctx.fetch=()=>new Promise(resolve=>resolveFetch=resolve);
 ctx.cur='raceA';let pending=ctx.show('raceA',true);ctx.cur='raceB';resolveFetch({ok:true,json:async()=>({race:{race_key:'raceA'}})});await pending;
 console.log('PASS: day error, failed cancellation, venue grouping and board, past days, confirmed cancellation, Japanese labels, stale detail response');
})().catch(e=>{console.error(e);process.exit(1)});
