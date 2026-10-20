// ===== ODDS SCOPE 地方：画面の枠（今日／過去／成績／その他／レース） =====
// 一覧は /api/day（軽量）だけで描く。レース画面は既存の show()/renderStatus() をそのまま使う。
const NS_WINDOWS={15:[13.75,15.72],10:[8.9,11.2],5:[4.6,6.2],4:[3.6,4.6],3:[1.85,3.6]};
const NS_SLOTS=[15,10,5,4,3];
const NS_VENUE_ORDER=['帯広','門別','盛岡','水沢','浦和','船橋','大井','川崎','金沢','笠松','名古屋','園田','姫路','高知','佐賀'];
const NS_WD=['日','月','火','水','木','金','土'];
const NS_MISS={SCHEDULER_NOT_RUNNING:'サーバー停止',RESERVED_AFTER_WINDOW:'予約が間に合わず',CANCELLED:'取消',NOT_SCHEDULED:'監視対象外',NOT_ATTEMPTED:'未実行',ERR:'取得エラー',SKIPPED_LATE:'時間切れ',INVALID_PAYLOAD:'旧形式'};
const NS={route:'',date:null,races:[],venue:null,back:'#/',timers:[],loading:false};
function nsStore(k,v){try{if(v===undefined)return sessionStorage.getItem(k);sessionStorage.setItem(k,v)}catch(e){return null}}
function nsToday(){return new Date().toLocaleDateString('sv-SE',{timeZone:'Asia/Tokyo'})}
function nsDayLabel(d){const x=new Date(d+'T00:00:00+09:00');return `${x.getMonth()+1}月${x.getDate()}日（${NS_WD[x.getDay()]}）`}
function nsHM(iso){return String(iso||'').slice(11,16)}
function nsMins(iso,now){return (new Date(iso)-now)/60000}
function nsVenues(races){const v=[...new Set(races.filter(r=>r.status!=='cancelled').map(r=>r.baba_name))];return v.sort((a,b)=>((NS_VENUE_ORDER.indexOf(a)+99)%99)-((NS_VENUE_ORDER.indexOf(b)+99)%99))}
function nsFinished(r,now){return r.status==='complete'||nsMins(r.start_iso,now)<-1}
function nsLamp(r,slot,now){
  if((r.slots||[]).includes(slot))return 'ok';
  if(r.status==='cancelled')return '';
  const m=nsMins(r.start_iso,now),[lo,hi]=NS_WINDOWS[slot];
  return m<lo?'miss':m<=hi?'open':'';
}
function nsLamps(r,now,lg){return `<span class="ns-lamps${lg?' lg':''}" aria-label="オッズ取得 ${(r.slots||[]).length}/5">${NS_SLOTS.map(s=>`<i class="${nsLamp(r,s,now)}">${s}</i>`).join('')}</span>`}
function nsNext(races,now){return races.filter(r=>r.status==='reserved'&&nsMins(r.start_iso,now)>0).sort((a,b)=>new Date(a.start_iso)-new Date(b.start_iso))[0]||null}
function nsMMSS(sec){sec=Math.max(0,Math.round(sec));const m=Math.floor(sec/60);return m>=60?`${Math.floor(m/60)}:${String(m%60).padStart(2,'0')}`:`${String(m).padStart(2,'0')}:${String(sec%60).padStart(2,'0')}`}

// 発走表：全場をレーンで並べ、発走点と今の時刻を1本の時間軸に置く
function nsBoard(races,now,isToday){
  const live=races.filter(r=>r.start_iso);if(!live.length)return '';
  const ts=live.map(r=>new Date(r.start_iso).getTime()),t0=Math.min(...ts)-20*60000,t1=Math.max(...ts)+10*60000;
  const X=t=>((t-t0)/(t1-t0)*100).toFixed(2)+'%';
  const next=isToday?nsNext(races,now):null,venues=nsVenues(races);
  const ticks=[];for(let t=Math.floor(t0/3600000)*3600000+3600000;t<t1;t+=3600000){const hr=(new Date(t).getUTCHours()+9)%24;if(hr%2===0||(t1-t0)<6*3600000)ticks.push(`<span style="left:${X(t)}">${hr}時</span>`)}
  const lanes=venues.map(v=>`<div class="ns-lane"><span class="ns-lane-name">${esc(v)}</span>${races.filter(r=>r.baba_name===v).map(r=>{
    const t=new Date(r.start_iso).getTime(),done=nsFinished(r,now);
    const cls=r.status==='cancelled'?'off':next&&r.race_key===next.race_key?'next':done?((r.slots||[]).length===5?'full':'done'):'';
    return `<button class="ns-pt ${cls}" style="left:${X(t)}" data-race="${esc(r.race_key)}" aria-label="${esc(v)}${r.race}R ${nsHM(r.start_iso)}${r.status==='cancelled'?' 取消':''}"></button>`}).join('')}</div>`).join('');
  const nowLine=isToday&&now.getTime()>t0&&now.getTime()<t1?`<i class="ns-now" style="left:${X(now.getTime())}"></i>`:'';
  return `<section class="ns-board" aria-label="発走表"><div class="ns-lanes">${lanes}${nowLine}</div><div class="ns-ticks">${ticks.join('')}</div>
  <div class="ns-legend"><span><i class="full"></i>5時点すべて取得</span><span><i class="done"></i>一部取得</span><span><i></i>これから</span></div></section>`;
}
function nsNextCard(r,now){
  if(!r)return '';
  const sec=(new Date(r.start_iso)-now)/1000;
  return `<button class="ns-next" data-race="${esc(r.race_key)}"><span class="k">次の発走</span><span class="v">${esc(r.baba_name)} ${r.race}R</span>
  <span class="c"><b id="nsCount">${nsMMSS(sec)}</b><small>${nsHM(r.start_iso)}発走まで</small></span>
  <span class="n">${esc(r.race_name||'')}${r.course?'　'+esc(r.course):''}</span><span class="l">${nsLamps(r,now,true)}</span></button>`;
}
function nsRow(r,now,isToday){
  const m=nsMins(r.start_iso,now),res=r.result,top=new Set(res?(res.order||[]).filter((h,i)=>(res.places||[])[i]<=3):[]);
  const picks=(r.picks||[]).length?`<span class="p">FINAL ${r.picks.map(h=>`<em class="${top.has(h)?'hit':''}">${h}</em>`).join('')}</span>`:'';
  let state;
  if(r.status==='cancelled')state='<b>—</b>取消';
  else if(res)state=`<b>${(res.order||[]).slice(0,3).join('-')}</b>結果`;
  else if(isToday&&m>0)state=`<b class="${m<=16?'soon':''}">${m<60?Math.ceil(m)+'分':nsHM(r.start_iso)}</b>${m<60?'発走まで':''}`;
  else state=m>-15?'<b>発走</b>結果待ち':'<b>—</b>結果待ち';
  const sub=[r.course,r.field_size?r.field_size+'頭':''].filter(Boolean).join('　');
  return `<button class="ns-race${r.status==='cancelled'?' off':''}" data-race="${esc(r.race_key)}"><span class="t"><b>${nsHM(r.start_iso)}</b><small>${r.race}R</small></span>
  <span class="m"><b>${esc(r.race_name||r.race+'R')}</b>${sub?`<span>${esc(sub)}</span>`:''}<span class="p">${nsLamps(r,now)}</span>${picks}</span><span class="s">${state}</span></button>`;
}
function nsDayHTML(date,races,now){
  const isToday=date===nsToday(),venues=nsVenues(races);
  if(!races.length)return `<h1 class="ns-day">${nsDayLabel(date)}<small>この日のレースはまだありません</small></h1><div class="ns-empty">開催日は公式の出走表から全レースを自動で予約します。開催がない日は、過去の開催を「過去」から見られます。</div>`;
  let v=NS.venue&&venues.includes(NS.venue)?NS.venue:null;
  if(!v){const nx=isToday?nsNext(races,now):null;v=nx?nx.baba_name:venues[0]}
  NS.venue=v;
  const mine=races.filter(r=>r.baba_name===v).sort((a,b)=>a.race-b.race);
  const open=isToday?mine.filter(r=>!nsFinished(r,now)):[],done=isToday?mine.filter(r=>nsFinished(r,now)):mine;
  const n=races.filter(r=>r.status!=='cancelled').length;
  return `<h1 class="ns-day">${nsDayLabel(date)}<small>${esc(venues.join('・'))}　${n}レース</small></h1>
  ${nsBoard(races,now,isToday)}${isToday?nsNextCard(nsNext(races,now),now):''}
  <div class="ns-seg" role="group" aria-label="競馬場">${venues.map(x=>`<button data-venue="${esc(x)}" aria-pressed="${x===v}">${esc(x)}<small>${races.filter(r=>r.baba_name===x&&r.status!=='cancelled').length}</small></button>`).join('')}</div>
  ${open.length?`<div class="ns-races">${open.map(r=>nsRow(r,now,isToday)).join('')}</div>`:''}
  ${done.length?(isToday?`<details class="ns-done"${open.length?'':' open'}><summary>終わったレース ${done.length}</summary><div class="ns-races">${done.map(r=>nsRow(r,now,isToday)).join('')}</div></details>`:`<div class="ns-races">${done.map(r=>nsRow(r,now,false)).join('')}</div>`):''}`;
}
async function nsFetchDay(date){
  const r=await fetch('/api/day?date='+encodeURIComponent(date)+'&t='+Date.now(),{cache:'no-store'});let j={};
  try{j=await r.json()}catch(e){}
  if(!r.ok||!j.ok)throw new Error(j.error||'レース一覧を取得できませんでした（HTTP '+r.status+'）');
  return j.races||[];
}
async function nsShowDay(date,quiet){
  const view=$('nsView');if(!quiet)view.innerHTML='<div class="ns-skel"></div>';
  try{const races=await nsFetchDay(date);if(NS.date!==date)return;NS.races=races;view.innerHTML=nsDayHTML(date,races,new Date())}
  catch(e){if(NS.date!==date)return;if(!quiet||!NS.races.length)view.innerHTML=`<div class="ns-err">${esc(e.message)}。少し待って右上の更新を押してください。</div>`}
}
async function nsShowDays(){
  const view=$('nsView');view.innerHTML='<div class="ns-skel"></div>';
  try{const r=await fetch('/api/days',{cache:'no-store'}),j=await r.json();if(!r.ok||!j.ok)throw new Error(j.error||'HTTP '+r.status);
    const today=nsToday(),days=(j.days||[]).filter(d=>d.date!=='2026-10-07');
    view.innerHTML=`<div class="ns-title"><h1>過去の開催</h1><p>日付を押すと、競馬場ごとのレースと結果を見られます。</p></div>${days.length?`<div class="ns-days">${days.map(d=>`<a class="ns-dayrow" href="${d.date===today?'#/':'#/past/'+esc(d.date)}"><b>${nsDayLabel(d.date)}${d.date===today?'　今日':''}</b><span>${esc(d.venues||'')}</span><em>${d.complete}/${d.races}</em></a>`).join('')}</div>`:'<div class="ns-empty">まだ記録された開催はありません。</div>'}`}
  catch(e){view.innerHTML=`<div class="ns-err">開催日の一覧を取得できませんでした：${esc(e.message)}</div>`}
}
async function nsHealth(){
  const el=$('nsHealth');if(!el)return;
  try{const r=await fetch('/api/capture-health?t='+Date.now(),{cache:'no-store'}),j=await r.json();if(!j.ok)throw new Error(j.error||'HTTP '+r.status);
    const s=j.summary||{},sc=j.scheduler||{},sy=j.schedule||{},miss=Object.entries(s.missed_by_reason||{}).map(([k,v])=>`${NS_MISS[k]||k} ${v}`).join('、')||'なし';
    el.innerHTML=`<dl><dt>今日の取得</dt><dd>${s.slots_ok??0}/${s.slots_due??0}時点（${s.success_rate==null?'—':Math.round(s.success_rate*100)+'%'}）</dd>
    <dt>取り逃しの内訳</dt><dd>${esc(miss)}</dd>
    <dt>取得処理</dt><dd class="${sc.stale?'ng':'ok'}">${sc.stale?'停止の疑い':'動いています'}（${sc.beat_age_s==null?'—':Math.round(sc.beat_age_s)+'秒前に確認'}）</dd>
    <dt>自動予約</dt><dd class="${sy.error?'ng':'ok'}">${sy.error?'エラー：'+esc(sy.error):sy.last_ok?nsHM(sy.last_ok)+'に取り込み':'未実行'}</dd>
    <dt>停止の記録</dt><dd>${(sc.gaps_today||[]).length?`${sc.gaps_today.length}回`:'なし'}</dd></dl>`}
  catch(e){el.innerHTML=`<div class="ns-err">取得の状態を確認できませんでした：${esc(e.message)}</div>`}
}
function nsSetNav(page){document.querySelectorAll('.ns-nav a').forEach(a=>a.setAttribute('aria-current',a.dataset.page===page?'page':'false'))}
function nsShowPage(which){
  ['nsView','nsRecord','nsMore','nsRaceBar'].forEach(id=>{const el=$(id);if(el)el.hidden=true});
  if(which!=='race'){const d=$('detail');if(d)d.style.display='none';if(typeof stopFlow==='function')stopFlow();cur=null}
  (which==='race'?['nsRaceBar']:[which]).forEach(id=>{const el=$(id);if(el)el.hidden=false});
}
async function nsRoute(){
  const h=decodeURIComponent(location.hash.replace(/^#\/?/,'')),[page,...rest]=h.split('/'),arg=rest.join('/');
  NS.route=h;
  if(page==='race'&&arg){nsShowPage('race');nsSetNav('');window.scrollTo(0,0);await show(arg);return}
  window.scrollTo(0,0);
  if(page==='record'){nsShowPage('nsRecord');nsSetNav('record');NS.back='#/record';learn();roiLoad();return}
  if(page==='more'){nsShowPage('nsMore');nsSetNav('more');NS.back='#/more';nsHealth();return}
  nsShowPage('nsView');
  if(page==='past'&&!arg){nsSetNav('past');NS.date=null;NS.back='#/past';await nsShowDays();return}
  const date=page==='past'?arg:nsToday();NS.date=date;NS.back=page==='past'?'#/past/'+date:'#/';nsSetNav(page==='past'?'past':'today');
  await nsShowDay(date);
}
// 既存コード（予約・取消・更新ボタン）から呼ばれる一覧の再読み込み
function load(){if(NS.date&&!$('nsView').hidden)return nsShowDay(NS.date,true);return Promise.resolve()}
function nsTick(){
  const now=new Date();updateClock();
  const c=$('nsCount');if(c&&NS.date===nsToday()&&!$('nsView').hidden){const nx=nsNext(NS.races,now);if(nx)c.textContent=nsMMSS((new Date(nx.start_iso)-now)/1000)}
}
document.addEventListener('click',e=>{
  const v=e.target.closest('[data-venue]');if(v&&$('nsView').contains(v)){NS.venue=v.dataset.venue;nsStore('nsVenue',NS.venue);if(NS.date)$('nsView').innerHTML=nsDayHTML(NS.date,NS.races,new Date());return}
  const r=e.target.closest('[data-race]');if(r&&!r.closest('#detail')){location.hash='#/race/'+encodeURIComponent(r.dataset.race)}
});
async function cancelR(k){
  if(!k||!confirm('このレースのオッズ記録をやめますか？（取得済みのデータは残ります）'))return;
  const btn=$('nsCancel');if(btn){btn.disabled=true;btn.textContent='処理中…'}
  try{
    const r=await fetch('/api/cancel',{method:'POST',headers:{'Content-Type':'application/json'},cache:'no-store',body:JSON.stringify({race_key:k})});
    const j=await r.json();
    if(!r.ok||!j.ok||j.status!=='cancelled')throw new Error(j.error||'HTTP '+r.status);
    if(cur===k){cur=null;nsStore('oddsScopeCur','');$('detail').style.display='none'}
    $('msg').textContent='記録をやめました';
    location.hash=NS.back||'#/';
    await load();
  }catch(e){
    alert('取り消せませんでした：'+e.message);$('msg').textContent='取り消せませんでした：'+e.message;
  }finally{if(btn){btn.disabled=false;btn.textContent='このレースの記録をやめる'}}
}
function rate(x){return x==null?'—':(x*100).toFixed(1)+'%'}
const NS_SYS={COLLECTING:['集計中','30レースまでは判定しません'],PASS:['人気を上回る','直近30レースで人気上位3頭より良い'],HOLD:['人気と同程度','直近30レースで人気上位3頭と同水準'],REVIEW:['人気を下回る','人気薄の昇格と学習の反映を自動で抑えています']};
const NS_MODEL={COLLECTING:'学習データを集めています',ACTIVE:'学習モデルを使用中',KEEP_HEURISTIC:'検証の結果、学習モデルは採用していません'};
async function learn(){try{
  const x=await(await fetch('/api/learning',{cache:'no-store'})).json(),rp=x.recent_performance||{},rf=x.recent_favorite||{},st=NS_SYS[x.system_status]||NS_SYS.COLLECTING;
  $('learn').innerHTML=`<h2 class="ns-h2">3着内に入った割合</h2><div class="ns-stats">
  <div class="ns-stat"><span>FINAL上位3頭</span><b>${rate(rp.pick_rate)}</b><small>直近${rp.races||0}レース・候補1頭ごと</small></div>
  <div class="ns-stat base"><span>人気上位3頭</span><b>${rate(rf.pick_rate)}</b><small>同じレースでの基準</small></div>
  <div class="ns-stat"><span>1頭以上が3着内</span><b>${rate(rp.race_hit_rate)}</b><small>人気上位3頭は ${rate(rf.race_hit_rate)}</small></div>
  <div class="ns-stat"><span>1位候補が3着内</span><b>${rate(rp.top1_place_rate)}</b><small>3頭とも3着内 ${rate(rp.perfect3_rate)}</small></div></div>
  <p class="ns-note">判定：<b>${st[0]}</b>（${st[1]}）。${NS_MODEL[x.model_status]||NS_MODEL.COLLECTING}：${x.learning_races||0}/${x.min_train_races||80}レース・${x.learning_samples||0}頭。学習モデルは、時系列の検証で今の計算式より良い場合だけ使います。</p>`;
 }catch(e){$('learn').innerHTML='<div class="ns-err">成績を取得できませんでした。少し待って更新してください。</div>'}}
function roiCell(v){if(!v||v.roi==null)return '<td>—</td>';const ci=v.ci95?`${rate(v.ci95[0])}〜${rate(v.ci95[1])}`:'区間は20レースから';return `<td><b class="${v.roi>=1?'up':'down'}">${rate(v.roi)}</b><small>的中 ${rate(v.hit_rate)}</small><small>${ci}</small></td>`}
async function roiLoad(){try{
  const x=await(await fetch('/api/roi',{cache:'no-store'})).json();if(!x.ok)throw new Error(x.error||'');
  $('roi').innerHTML=`<section class="ns-block"><h2 class="ns-h2">回収率（公式の払戻金）</h2><p class="ns-note">${x.races||0}レース、1点100円で計算。100%以上が緑。下の段は95%の範囲で、100%をまたぐ間は偶然と区別できません。</p>
  <table class="ns-tbl"><thead><tr><th>買い方（3頭）</th><th>FINAL上位3</th><th>人気上位3</th></tr></thead><tbody>${(x.strategies||[]).map(s=>`<tr><td>${esc(s.label)}</td>${roiCell(s.scope)}${roiCell(s.favorite)}</tr>`).join('')}</tbody></table></section>
  <section class="ns-block"><h2 class="ns-h2">締切直前の売れ方</h2><p class="ns-note">3分前から確定までに単勝オッズがどう動いた馬が走ったか（${x.late_money_races||0}レース・${x.late_money_horses||0}頭）。人気の差も含むため、傾向の確認用です。</p>
  <table class="ns-tbl"><thead><tr><th>動き</th><th>頭数</th><th>3着内率</th><th>複勝回収率</th></tr></thead><tbody>${(x.late_money||[]).map(b=>`<tr><td>${esc(b.label)}</td><td>${b.horses||0}</td><td>${rate(b.place_rate)}</td><td><b class="${(b.place_roi||0)>=1?'up':'down'}">${rate(b.place_roi)}</b></td></tr>`).join('')}</tbody></table>
  <p class="ns-note">急に売れた＝確定オッズが3分前より20%以上下がった馬。</p></section>`;
 }catch(e){$('roi').innerHTML='<div class="ns-err">回収率を取得できませんでした。</div>'}}
let refreshBusy=false;
async function refreshHeaderData(){
  if(refreshBusy)return;refreshBusy=true;const b=$('headerRefresh');b.classList.add('spin');
  try{await Promise.all([NS.route.startsWith('race/')&&cur?show(cur,true):nsRoute(),...(NS.route==='record'?[learn(),roiLoad()]:[])])}catch(e){}
  finally{refreshBusy=false;setTimeout(()=>b.classList.remove('spin'),400)}
}
// ---- boot ----
$('nsBack').addEventListener('click',e=>{e.preventDefault();location.hash=NS.back||'#/'});
$('nsCancel').addEventListener('click',()=>cancelR(cur));
window.addEventListener('hashchange',nsRoute);
NS.venue=nsStore('nsVenue')||null;
nsRoute();updateClock();
setInterval(nsTick,1000);
setInterval(()=>{if(!document.hidden&&NS.date&&!$('nsView').hidden)nsShowDay(NS.date,true)},15000);
setInterval(()=>{if(cur&&NS.route.startsWith('race/'))show(cur,true)},20000);
setInterval(()=>{if(!document.hidden&&NS.route==='record'){learn();roiLoad()}},60000);
