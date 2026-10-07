const fs=require('fs'),vm=require('vm'),assert=require('assert');
const html=fs.readFileSync(__dirname+'/index.html','utf8');
function extract(a,b){return html.slice(html.indexOf(a),html.indexOf(b,html.indexOf(a)));}
(async()=>{
 const elements={list:{innerHTML:'previous'},msg:{textContent:''},detail:{style:{display:'block'}},confidence:{},};
 const ctx={fetch:async()=>({ok:false,status:503,json:async()=>({error:'接続失敗'})}),$:id=>elements[id],esc:String,confirm:()=>true,cur:'raceA',sessionStorage:{setItem(){},removeItem(){throw Error('must not clear failed cancel')}},encodeURIComponent,renderStatus(){throw Error('stale response rendered')}};
 vm.createContext(ctx);
 vm.runInContext(extract('async function load()','async function refreshProfile(')+extract('async function cancelR(k)','document.addEventListener'),ctx);
 await ctx.load();assert(elements.list.innerHTML.includes('一覧取得失敗'));assert(!elements.list.innerHTML.includes('予約中のレースはありません'));
 await ctx.cancelR('raceA');assert.equal(ctx.cur,'raceA');assert.equal(elements.detail.style.display,'block');assert(elements.msg.textContent.includes('取消できませんでした'));
 vm.runInContext(extract('async function show(k,silent=false)','async function cancelR(k)'),ctx);
 let resolveFetch;ctx.fetch=()=>new Promise(resolve=>resolveFetch=resolve);
 let pending=ctx.show('raceA',true);ctx.cur='raceB';resolveFetch({ok:true,json:async()=>({race:{race_key:'raceA'}})});await pending;
 console.log('PASS: list error, failed cancellation, stale detail response');
})().catch(e=>{console.error(e);process.exit(1)});
