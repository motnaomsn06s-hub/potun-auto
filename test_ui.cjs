const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
function extract(a,b){const i=html.indexOf(a);assert(i>=0,'missing '+a);return html.slice(i,html.indexOf(b,i));}
(async()=>{
 const elements={list:{innerHTML:'previous',querySelectorAll:()=>[]},msg:{textContent:''},detail:{style:{display:'block'}},confidence:{}};
 const alerts=[];
 const ctx={fetch:async()=>({ok:false,status:503,json:async()=>({error:'接続失敗'})}),$:id=>elements[id]||null,esc:String,confirm:()=>true,alert:m=>alerts.push(m),
  cur:'raceA',sessionStorage:{setItem(){},removeItem(){}},encodeURIComponent,decodeURIComponent,Date,chip:()=>'',
  document:{querySelectorAll:()=>[]},renderStatus(){throw Error('stale response rendered')}};
 vm.createContext(ctx);
 vm.runInContext(extract('let queueOpenDates','async function refreshProfile(')+extract('async function cancelR(k)','document.addEventListener'),ctx);

 // 1. A failed list request shows an error and never pretends the list is empty.
 await ctx.load();
 assert(elements.list.innerHTML.includes('一覧取得失敗'));
 assert(!elements.list.innerHTML.includes('予約中のレースはありません'));

 // 2. Network failure on cancel ("Load failed" on iPhone) is reported as failure, nothing is cleared.
 ctx.fetch=async()=>{throw new TypeError('Load failed')};
 ctx.sessionStorage.removeItem=()=>{throw Error('must not clear failed cancel')};
 await ctx.cancelR('raceA');
 assert.equal(ctx.cur,'raceA');assert.equal(elements.detail.style.display,'block');
 assert(elements.msg.textContent.includes('失敗'));assert(alerts.some(a=>a.includes('Load failed')));

 // 3. Paging follows next_before and concatenates older races; 2026-10-07 is excluded from display.
 const pages={'':{races:[{race_key:'2026-10-18:31:1',date:'2026-10-18',baba_name:'高知',race:1,start_iso:'2026-10-18T15:00:00+09:00',status:'reserved'}],has_more:true,next_before:'c1'},
              'c1':{races:[{race_key:'2026-10-07:21:1',date:'2026-10-07',baba_name:'川崎',race:1,start_iso:'2026-10-07T15:00:00+09:00',status:'complete'},
                           {race_key:'2026-10-05:21:2',date:'2026-10-05',baba_name:'川崎',race:2,start_iso:'2026-10-05T15:00:00+09:00',status:'complete'}],has_more:false,next_before:null}};
 const urls=[];
 ctx.fetch=async u=>{urls.push(u);const m=/before=([^&]+)/.exec(u);return {ok:true,status:200,json:async()=>pages[m?decodeURIComponent(m[1]):'']}};
 const got=await ctx.fetchRaces(120);
 assert.equal(got.races.length,3);assert.equal(got.more,false);assert(urls[1].includes('before=c1'));
 ctx.queueLimit=120;await ctx.load();
 assert(elements.list.innerHTML.includes('2026-10-18'));assert(elements.list.innerHTML.includes('2026-10-05'));
 assert(!elements.list.innerHTML.includes('2026-10-07'));

 // 4. Successful cancel requires the server to confirm status=cancelled.
 ctx.sessionStorage.removeItem=()=>{};
 ctx.fetch=async u=>u.startsWith('/api/cancel')?{ok:true,status:200,json:async()=>({ok:true,removed:true,status:'cancelled'})}:{ok:true,status:200,json:async()=>pages['c1']};
 await ctx.cancelR('raceA');
 assert.equal(ctx.cur,null);assert(elements.msg.textContent.includes('取り消しました'));

 // 5. Stale detail responses are not rendered after switching race.
 vm.runInContext(extract('async function show(k,silent=false)','async function cancelR(k)'),ctx);
 let resolveFetch;ctx.fetch=()=>new Promise(resolve=>resolveFetch=resolve);
 ctx.cur='raceA';let pending=ctx.show('raceA',true);ctx.cur='raceB';resolveFetch({ok:true,json:async()=>({race:{race_key:'raceA'}})});await pending;
 console.log('PASS: list error, failed cancellation, paging, confirmed cancellation, stale detail response');
})().catch(e=>{console.error(e);process.exit(1)});
