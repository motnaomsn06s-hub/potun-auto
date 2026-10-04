from flask import Flask,request,jsonify,send_from_directory
import requests,re,math,os,json
import psycopg
from psycopg.rows import dict_row
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta

app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0 (compatible; OddsScopeNAR/10.3)"}
JST=timezone(timedelta(hours=9))
DATABASE_URL=os.environ.get("DATABASE_URL")

def con():
 if not DATABASE_URL:
  raise RuntimeError("DATABASE_URL is not configured")
 return psycopg.connect(DATABASE_URL,row_factory=dict_row,connect_timeout=15)

def init():
 c=con()
 schema="""
 CREATE TABLE IF NOT EXISTS races(race_key TEXT PRIMARY KEY,date TEXT,baba TEXT,baba_name TEXT,race INTEGER,start_iso TEXT,status TEXT DEFAULT 'reserved',created_at TEXT,result_checked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS snapshots(race_key TEXT,slot INTEGER,fetched_at TEXT,payload TEXT,PRIMARY KEY(race_key,slot));
 CREATE TABLE IF NOT EXISTS predictions(race_key TEXT,horse INTEGER,rank INTEGER,score DOUBLE PRECISION,pop INTEGER,odds DOUBLE PRECISION,d1 DOUBLE PRECISION,d2 DOUBLE PRECISION,agree DOUBLE PRECISION,created_at TEXT,PRIMARY KEY(race_key,horse));
 CREATE TABLE IF NOT EXISTS results(race_key TEXT PRIMARY KEY,first_horse INTEGER,second_horse INTEGER,third_horse INTEGER,fetched_at TEXT,payload TEXT);
 CREATE TABLE IF NOT EXISTS learning_samples(
   race_key TEXT,horse INTEGER,label INTEGER,
   base15 DOUBLE PRECISION,base10 DOUBLE PRECISION,base5 DOUBLE PRECISION,d1 DOUBLE PRECISION,d2 DOUBLE PRECISION,agree DOUBLE PRECISION,persist DOUBLE PRECISION,
   odds15 DOUBLE PRECISION,odds10 DOUBLE PRECISION,odds5 DOUBLE PRECISION,win_flow1 DOUBLE PRECISION,win_flow2 DOUBLE PRECISION,win_move DOUBLE PRECISION,
   odds DOUBLE PRECISION,pop INTEGER,heuristic DOUBLE PRECISION,created_at TEXT,
   PRIMARY KEY(race_key,horse)
 );
 CREATE TABLE IF NOT EXISTS model_state(
   id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER DEFAULT 0,status TEXT DEFAULT 'COLLECTING',
   trained_races INTEGER DEFAULT 0,trained_samples INTEGER DEFAULT 0,
   weights TEXT,means TEXT,stds TEXT,
   heuristic_val DOUBLE PRECISION,learned_val DOUBLE PRECISION,updated_at TEXT
 );
 CREATE TABLE IF NOT EXISTS app_meta(k TEXT PRIMARY KEY,v TEXT);
 CREATE TABLE IF NOT EXISTS profiles(
   race_key TEXT,horse INTEGER,payload TEXT,fetched_at TEXT,
   PRIMARY KEY(race_key,horse)
 );
 """
 for stmt in schema.split(";"):
  if stmt.strip(): c.execute(stmt)
 c.execute("INSERT INTO model_state(id,status) VALUES(1,'COLLECTING') ON CONFLICT(id) DO NOTHING")
 c.commit();c.close()
 # PostgreSQL migration. Version 10 adds official NAR performance profiles while preserving market learning.
 c=con()
 for col,typ in (("parser_version","INTEGER DEFAULT 1"),("odds15","DOUBLE PRECISION"),("odds10","DOUBLE PRECISION"),("odds5","DOUBLE PRECISION"),
                 ("win_flow1","DOUBLE PRECISION"),("win_flow2","DOUBLE PRECISION"),("win_move","DOUBLE PRECISION")):
  try:
   c.execute(f"ALTER TABLE learning_samples ADD COLUMN {col} {typ}");c.commit()
  except Exception:c.rollback()
 c.close()
 c=con();done=c.execute("SELECT v FROM app_meta WHERE k='analysis_v10_scope_hybrid'").fetchone()
 if not done:
  c.execute("UPDATE model_state SET status='COLLECTING',trained_races=0,trained_samples=0,weights=NULL,means=NULL,stds=NULL,heuristic_val=NULL,learned_val=NULL,updated_at=%s WHERE id=1",(datetime.now(JST).isoformat(),))
  c.execute("INSERT INTO app_meta(k,v) VALUES('analysis_v10_scope_hybrid','1') ON CONFLICT(k) DO UPDATE SET v='1'")
 c.commit();c.close()
init()

def soup(path,q):
 r=requests.get(BASE+path,params=q,headers=UA,timeout=20);r.raise_for_status();r.encoding=r.apparent_encoding or r.encoding
 return BeautifulSoup(r.text,"html.parser")
def qfor(r):return {"k_babaCode":r["baba"],"k_raceDate":r["date"].replace("-","/"),"k_raceNo":r["race"]}
def _num(text):
 m=re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*",(text or "").replace(",",""))
 return float(m.group(1)) if m else None

def win(s):
 # NAR default 馬番順: 枠 / 馬番 / 馬名 / 単勝 / 複勝...
 # NAR 人気順:       人気 / 枠 / 馬番 / 馬名 / 単勝 / 複勝...
 # Explicitly read only the 単勝 cell. Never scan 複勝 cells.
 out={}
 for tr in s.find_all("tr"):
  cells=[" ".join(x.stripped_strings) for x in tr.find_all(["td","th"])]
  if len(cells)<4:continue
  isint=lambda x: bool(re.fullmatch(r"\d{1,2}",x or ""))
  h=o=None
  if len(cells)>=5 and isint(cells[0]) and isint(cells[1]) and isint(cells[2]):
   cand=int(cells[2]);price=_num(cells[4])
   if 1<=cand<=18 and price is not None and price>=1:h,o=cand,price
  elif isint(cells[0]) and isint(cells[1]):
   cand=int(cells[1]);price=_num(cells[3])
   if 1<=cand<=18 and price is not None and price>=1:h,o=cand,price
  if h is not None:out[h]=o
 return [[h,out[h]] for h in sorted(out)]
def combo(s,n):
 t=s.get_text(" ",strip=True).replace("→","-").replace("－","-")
 pat=r"(?<!\d)(\d{1,2})\s*-\s*(\d{1,2})"+(r"\s*-\s*(\d{1,2})" if n==3 else "")+r"\s+([\d,.]+)"
 d={}
 for m in re.finditer(pat,t):
  g=m.groups();hs=tuple(map(int,g[:n]))
  try:o=float(g[n].replace(",",""))
  except:continue
  if o>0:d[hs]=list(hs)+[o]
 return list(d.values())
def med(a):
 b=sorted(a);n=len(b);return 0 if not n else b[n//2] if n%2 else (b[n//2-1]+b[n//2])/2
def rz(a):
 if not a:return []
 m=med(a);mad=med([abs(x-m) for x in a]);return [0]*len(a) if mad<1e-9 else [(x-m)/(1.4826*mad) for x in a]

def band_rz(values,model_probs):
 # Compare a combination only with combinations of similar theoretical probability.
 # This removes the systematic tendency for very-low-probability combinations to look
 # "overbet" merely because real markets are flatter than a pure win-odds model.
 n=len(values)
 if not n:return []
 global_z=rz(values)
 order=sorted(range(n),key=lambda i:model_probs[i])
 bins=min(8,max(3,int(math.sqrt(n))))
 out=[0.0]*n
 for b in range(bins):
  ids=order[b*n//bins:(b+1)*n//bins]
  if not ids:continue
  vals=[values[i] for i in ids];m=med(vals);mad=med([abs(x-m) for x in vals])
  for i in ids:
   z=global_z[i] if mad<1e-7 else (values[i]-m)/(1.4826*mad)
   out[i]=max(-4.0,min(4.0,z))
 return out

def agg(a):
 # Do not keep only the three positive outliers. Use the whole distribution,
 # including negative evidence, and reward broad/consistent distortion instead.
 if not a:return 0.0
 v=sorted(max(-4.0,min(4.0,float(x))) for x in a)
 n=len(v);trim=int(n*.10)
 core=v[trim:n-trim] if n-2*trim>=3 else v
 center=sum(core)/len(core)
 q=max(1,int(math.ceil(n*.25)))
 tails=(sum(v[:q])/q + sum(v[-q:])/q)/2
 pos=sum(x>.5 for x in v)/n;neg=sum(x<-.5 for x in v)/n
 breadth=pos-neg
 return .65*center+.20*tails+.15*breadth

def tail_reliability(pop,odds):
 # Soft confidence adjustment only; never hard-excludes a longshot.
 # Extreme longshots need stronger anomaly evidence before receiving the same
 # candidate score as a horse with a materially higher baseline chance.
 p=max(0,int(pop)-8)*.06
 o=max(0.0,math.log(max(float(odds),1.0)/40.0))*.10
 return max(.70,min(1.0,1.0-p-o))

def analyse(W,Q,E,T):
 inv={h:1/o for h,o in W};sm=sum(inv.values());P={h:v/sm for h,v in inv.items()};od=dict(W)
 pop={h:i+1 for i,(h,o) in enumerate(sorted(W,key=lambda x:(x[1],x[0])))}
 B={h:{k:[] for k in "QET"} for h in P}
 def market(rows,k):
  mod=[];act=[];hh=[]
  for a in rows:
   hs,o=a[:-1],a[-1]
   try:
    if k=="Q":
     i,j=hs;pr=P[i]*P[j]/max(1-P[i],1e-9)+P[j]*P[i]/max(1-P[j],1e-9)
    elif k=="E":
     i,j=hs;pr=P[i]*P[j]/max(1-P[i],1e-9)
    else:
     i,j,z=hs;pr=P[i]*P[j]/max(1-P[i],1e-9)*P[z]/max(1-P[i]-P[j],1e-9)
   except:continue
   if pr>0 and o>0:mod.append(pr);act.append(1/o);hh.append(hs)
  if not hh:return
  ms=sum(mod);aa=sum(act)
  modelp=[m/ms for m in mod];actualp=[a/aa for a in act]
  D=[math.log(max(ap,1e-15)/max(mp,1e-15)) for ap,mp in zip(actualp,modelp)]
  Z=band_rz(D,modelp)
  for z,hs in zip(Z,hh):
   for h in hs:
    if h in B:B[h][k].append(z)
 market(Q,"Q");market(E,"E");market(T,"T")
 out=[]
 for h,b in B.items():
  q,e,t=agg(b["Q"]),agg(b["E"]),agg(b["T"])
  out.append({"horse":h,"odds":od[h],"pop":pop[h],"Q":q,"E":e,"T":t,
              "base":(q+e+t)/3,"reliability":tail_reliability(pop[h],od[h])})
 return out
DATA_VERSION=6



PROFILE_VERSION=3

def _record_score(rec):
 w,s,t,o=rec;starts=w+s+t+o
 if starts<=0:return None
 raw=100.0*(w+.62*s+.38*t)/starts
 return max(0.0,min(100.0,(raw*starts+50.0*3)/(starts+3)))

def _place_score(finish,field):
 try:
  finish=int(finish);field=int(field)
  if field<=1:return 50.0
  return max(0.0,min(100.0,100.0*(field-finish)/(field-1)))
 except:return None

def _form_score(runs):
 vals=[];weights=(.40,.30,.20,.10)
 for i,r in enumerate(runs[:4]):
  ps=_place_score(r.get("finish"),r.get("field"))
  if ps is not None:vals.append((weights[i],ps))
 if not vals:return None
 return sum(w*v for w,v in vals)/sum(w for w,_ in vals)

def _distance_score(runs,current_distance):
 if not current_distance:return None
 vals=[];weights=(.40,.30,.20,.10)
 for i,r in enumerate(runs[:4]):
  ps=_place_score(r.get("finish"),r.get("field"));d=r.get("distance")
  if ps is None or not d:continue
  close=math.exp(-abs(float(d)-float(current_distance))/350.0)
  w=weights[i]*max(.18,close)
  vals.append((w,ps))
 if not vals:return None
 return sum(w*v for w,v in vals)/sum(w for w,_ in vals)

def _style_from_runs(runs):
 lasts=[]
 for r in runs[:4]:
  c=r.get("corners") or []
  if c:lasts.append(c[-1])
 if not lasts:return ("不明",None)
 a=sum(lasts)/len(lasts)
 if a<=2.7:return ("逃・先",68.0)
 if a<=5.2:return ("先・好位",61.0)
 if a<=8.0:return ("中団",55.0)
 return ("差・追",52.0)

def _condition_score(runs,body_diff):
 parts=[]
 if len(runs)>=2:
  a=_place_score(runs[0].get("finish"),runs[0].get("field"));b=_place_score(runs[1].get("finish"),runs[1].get("field"))
  if a is not None and b is not None:parts.append(max(20.0,min(80.0,50.0+(a-b)*.35)))
 if body_diff is not None:
  d=abs(body_diff);parts.append(62.0 if d<=8 else 54.0 if d<=14 else 43.0 if d<=24 else 32.0)
 if not parts:return None
 return sum(parts)/len(parts)

def _find_jockey(text):
 # Weight is immediately followed by the current jockey on NAR DebaTableSmall.
 m=re.search(r"(?:★|▲|△|☆|◇)?\s*\d{2,3}(?:\.\d)?\s+([一-龠々ヶァ-ンー]{2,12})\s*[（(][^）)]{0,24}[）)]",text)
 return m.group(1).strip() if m else ""

def _runs_from_text(text):
 # Official NAR format: venueMM.DD condition direction distance ... finish/field ... time ... corners ...
 marks=list(re.finditer(r"([一-龠々ヶァ-ンー]{1,10})\s*(\d{2}\.\d{2})\s+",text))
 runs=[]
 for i,m in enumerate(marks[:5]):
  seg=text[m.start():(marks[i+1].start() if i+1<len(marks) else len(text))]
  md=re.search(r"(?:左|右|直)\s*(\d{3,4})",seg)
  mf=re.search(r"(?<!\d)(\d{1,2})\s*/\s*(\d{1,2})(?!\d)",seg)
  if "出走取消" in seg and not mf:continue
  corners=[]
  seqs=re.findall(r"(?<!\d)((?:\d{1,2}-){1,3}\d{1,2})(?!\d)",seg)
  if seqs:
   vals=[int(v) for v in seqs[-1].split('-')]
   if all(1<=v<=18 for v in vals):corners=vals
  runs.append({"venue":m.group(1),"distance":int(md.group(1)) if md else None,
               "finish":int(mf.group(1)) if mf else None,"field":int(mf.group(2)) if mf else None,"corners":corners})
 return runs

def parse_deba(s,race=None):
 """Parse only values actually printed by NAR DebaTableSmall/DebaTable.
 Missing values are stored as None and are never displayed as a fake neutral 50.
 """
 page_text=s.get_text(" ",strip=True)
 md=re.search(r"(?:ダート|芝)?\s*(\d{3,4})ｍ",page_text)
 current_distance=int(md.group(1)) if md else None
 out={};last_frame=None
 for tr in s.find_all("tr"):
  cells=[" ".join(x.stripped_strings) for x in tr.find_all(["td","th"])]
  if len(cells)<3:continue
  isint=lambda x:bool(re.fullmatch(r"\d{1,2}",x or ""))
  frame=horse=None;name=None
  if isint(cells[0]) and len(cells)>=2 and isint(cells[1]):
   f,h=int(cells[0]),int(cells[1])
   if 1<=f<=8 and 1<=h<=18:frame,horse,last_frame=f,h,f;name=cells[2].strip()
  elif isint(cells[0]) and last_frame is not None:
   h=int(cells[0])
   if 1<=h<=18 and len(cells)>1 and not isint(cells[1]):frame,horse=last_frame,h;name=cells[1].strip()
  if horse is None or not name or len(name)<2:continue
  # The pedigree cell can contain sire/sex-age/horse/mare. Extract the horse name after sex-age.
  mn=re.search(r"(?:牡|牝|セン|セ)\s*\d+\s+([^\s]+)",name)
  if mn:name=mn.group(1).strip()
  text=" ".join(cells)
  # A real horse row contains current jockey record/body weight and/or past-race dates.
  rec_matches=list(re.finditer(r"(?<!\d)(\d+)\s*-\s*(\d+)\s*-\s*(\d+)\s*-\s*(\d+)(?!\d)",text))
  recs=[tuple(map(int,m.groups())) for m in rec_matches]
  runs=_runs_from_text(text)
  if not recs and not runs:
   if horse in out:continue
  jockey=_find_jockey(text)
  jrec=recs[0] if recs else (0,0,0,0)
  # Printed order after jockey record: overall / left / right / current venue.
  overall=recs[1] if len(recs)>1 else (0,0,0,0)
  left_rec=recs[2] if len(recs)>2 else (0,0,0,0)
  right_rec=recs[3] if len(recs)>3 else (0,0,0,0)
  course=recs[4] if len(recs)>4 else (0,0,0,0)
  bw=bd=None
  if rec_matches:
   tail=text[rec_matches[0].end():rec_matches[0].end()+30]
   mb=re.search(r"(?<!\d)(\d{3})\s+([+-]?\d+)(?!\d)",tail)
   if mb:bw,bd=int(mb.group(1)),int(mb.group(2))
  style,style_base=_style_from_runs(runs)
  form=_form_score(runs);dist=_distance_score(runs,current_distance);crs=_record_score(course)
  jockey_score=_record_score(jrec);cond=_condition_score(runs,bd)
  out[horse]={"profile_version":PROFILE_VERSION,"frame":frame,"horse":horse,"name":name,
   "jockey":jockey,"jockey_record":list(jrec),"jockey_has_data":jockey_score is not None,
   "overall_record":list(overall),"left_record":list(left_rec),"right_record":list(right_rec),"course_record":list(course),
   "recent_runs":runs,"recent_finishes":[x.get("finish") for x in runs if x.get("finish") is not None],
   "style":style,"style_base":round(style_base,1) if style_base is not None else None,
   "body_weight":bw,"body_diff":bd,"form_score":round(form,1) if form is not None else None,
   "distance_score":round(dist,1) if dist is not None else None,"course_score":round(crs,1) if crs is not None else None,
   "jockey_score":round(jockey_score,1) if jockey_score is not None else None,
   "condition_score":round(cond,1) if cond is not None else None,"current_distance":current_distance}
 return out

def load_profiles(key):
 c=con();rows=c.execute("SELECT horse,payload FROM profiles WHERE race_key=%s",(key,)).fetchall();c.close()
 out={}
 for x in rows:
  try:
   p=json.loads(x["payload"])
   if int(p.get("profile_version") or 0)>=PROFILE_VERSION:out[int(x["horse"])]=p
  except:pass
 return out

def get_profiles(r,force=False):
 old=load_profiles(r["race_key"])
 if old and not force:return old
 try:
  prof={}
  for path in ("DebaTableSmall","DebaTable"):
   try:
    prof=parse_deba(soup(path,qfor(r)),r)
    if prof:break
   except Exception:
    prof={}
  if prof:
   now=datetime.now(JST).isoformat();c=con()
   for h,p in prof.items():
    c.execute("INSERT INTO profiles(race_key,horse,payload,fetched_at) VALUES(%s,%s,%s,%s) ON CONFLICT(race_key,horse) DO UPDATE SET payload=EXCLUDED.payload,fetched_at=EXCLUDED.fetched_at",
              (r["race_key"],h,json.dumps(p,ensure_ascii=False),now))
   c.commit();c.close();return prof
 except Exception:pass
 return old

def _pace_fit(style,front_count):
 if style=="逃・先":return 72.0 if front_count<=2 else (58.0 if front_count==3 else 43.0)
 if style=="先・好位":return 64.0 if front_count<=3 else 70.0
 if style=="中団":return 56.0 if front_count<=3 else 64.0
 if style=="差・追":return 48.0 if front_count<=2 else (60.0 if front_count==3 else 70.0)
 return None

def take(r):
 q=qfor(r);W=win(soup("OddsTanFuku",q));Q=combo(soup("OddsUmLenFuku",q),2);E=combo(soup("OddsUmLenTan",q),2);T=combo(soup("Odds3LenTan",q),3)
 cnt={"win":len(W),"Q":len(Q),"E":len(E),"T":len(T)}
 if len(W)<3 or min(len(Q),len(E),len(T))==0:raise RuntimeError("オッズ取得不完全 "+str(cnt))
 market_sum=sum(1.0/o for _,o in W if o>0)
 if not 0.55<=market_sum<=2.20:raise RuntimeError(f"単勝オッズ検証NG market_sum={market_sum:.3f} counts={cnt}")
 return {"rows":analyse(W,Q,E,T),"counts":cnt,"win_market_sum":round(market_sum,4),"parser_version":DATA_VERSION,"fetched_at":datetime.now(JST).isoformat(timespec="seconds")}

def valid_payload(p):
 return isinstance(p,dict) and int(p.get("parser_version") or 0)>=DATA_VERSION

def _win_flow(prev_odds,cur_odds):
 if not prev_odds or not cur_odds or prev_odds<=0 or cur_odds<=0:return 0.0
 return math.log(prev_odds/cur_odds)  # positive = odds shortened / money entered

def point_signals(payload,prev=None):
 rows=payload.get("rows",[]);pm={str(x["horse"]):x for x in (prev or {}).get("rows",[])}
 raw=[];flows=[]
 for x in rows:
  p=pm.get(str(x["horse"])) if pm else None
  flows.append(_win_flow(p.get("odds") if p else None,x.get("odds")) if p else 0.0)
 zflows=rz(flows) if prev else [0.0]*len(rows)
 for x,wz,wf in zip(rows,zflows,flows):
  agree=sum(v>0 for v in (x["Q"],x["E"],x["T"]))/3
  level=math.tanh(max(0,x["base"])/2)
  p=pm.get(str(x["horse"])) if pm else None
  delta=x["base"]-(p["base"] if p else x["base"])
  anomaly_move=math.tanh(max(0,delta)/1.5) if p else 0
  win_abs=math.tanh(max(0,wf)/0.30) if p else 0
  win_rel=math.tanh(max(0,wz)/2.0) if p else 0
  win_signal=.45*win_abs+.55*win_rel
  raw_score=100*((.70 if not p else .42)*level+(.0 if not p else .10)*anomaly_move+(.0 if not p else .38)*win_signal+.10*agree)
  rel=float(x.get("reliability",tail_reliability(x["pop"],x["odds"])))
  score=round(raw_score*rel)
  pct=((p["odds"]/x["odds"])-1.0)*100 if p and x.get("odds") else None
  raw.append({**x,"delta":delta,"agree":agree,"reliability":rel,"score":max(0,min(100,score)),
              "prev_odds":p.get("odds") if p else None,"win_flow":wf,"win_pct":pct,"win_flow_z":wz})
 return sorted(raw,key=lambda x:-x["score"])[:3]

def rank_features(A,model_state=None):
 out=[];state=model_state if model_state is not None else get_model_state()
 for src in A:
  x=dict(src);lp=model_score(x,state)
  x["learned_prob"]=lp;x["rank_score"]=float(x.get("scope_score") or 0)/100.0;x["score"]=x.get("scope_score") or 0
  out.append(x)
 return sorted(out,key=lambda x:(-x["rank_score"],x.get("pop",99)))

def predictions(key,model_state=None):
 return rank_features(final_features(key),model_state)

def scope_label(x):
 total=float(x.get("scope_score") or 0);flow=float(x.get("flow_score") or x.get("market_score") or 0)
 edge=x.get("edge_score");conf=float(x.get("data_confidence") or 0)
 if edge is not None and total>=76 and edge>=66 and flow>=55 and conf>=55:return "CORE"
 if edge is not None and total>=68 and edge>=58 and flow>=48:return "VALUE"
 if total>=60 and flow>=45:return "WATCH"
 return "NO SIGNAL"

def adaptive_candidates(A):
 C=[x for x in A if scope_label(x)!="NO SIGNAL"]
 return C[:3],None

def save_predictions(key):
 A=predictions(key);C,_=adaptive_candidates(A)
 c=con();c.execute("DELETE FROM predictions WHERE race_key=%s",(key,))
 for i,x in enumerate(C,1):c.execute("INSERT INTO predictions VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(race_key,horse) DO UPDATE SET rank=EXCLUDED.rank,score=EXCLUDED.score,pop=EXCLUDED.pop,odds=EXCLUDED.odds,d1=EXCLUDED.d1,d2=EXCLUDED.d2,agree=EXCLUDED.agree,created_at=EXCLUDED.created_at",(key,x["horse"],i,x["score"],x["pop"],x["odds"],x["d1"],x["d2"],x["agree"],datetime.now(JST).isoformat()))
 c.commit();c.close();return C

def signal_strength(x):
 return scope_label(x)

FEATURES=("base15","base10","base5","d1","d2","agree","persist","win_flow1","win_flow2","win_move","log_odds","pop_scaled")
MIN_TRAIN_RACES=30
MIN_TRAIN_SAMPLES=200

def _sigmoid(z):
 z=max(-35.0,min(35.0,z))
 return 1.0/(1.0+math.exp(-z))

def _weighted_available(items):
 vals=[(float(v),float(w)) for v,w in items if v is not None]
 if not vals:return None,0.0
 return sum(v*w for v,w in vals)/sum(w for _,w in vals),100.0*sum(w for _,w in vals)/sum(w for _,w in items)

def final_features(key):
 c=con();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(key,)).fetchall();c.close()
 S={str(x["slot"]):json.loads(x["payload"]) for x in ss};S={k:v for k,v in S.items() if valid_payload(v)}
 if not all(k in S for k in ("15","10","5")):return []
 def mp(k):return {str(x["horse"]):x for x in S[k].get("rows",[])}
 a,b,z=mp("15"),mp("10"),mp("5");raw=[]
 profiles=load_profiles(key)
 front_count=sum(1 for p in profiles.values() if p.get("style") in ("逃・先","先・好位"))
 for h,x in z.items():
  if h not in a or h not in b:continue
  d1=b[h]["base"]-a[h]["base"];d2=x["base"]-b[h]["base"]
  persist=sum(v["base"]>0 for v in (a[h],b[h],x))/3
  agree=sum(v>0 for v in (x["Q"],x["E"],x["T"]))/3
  o15=float(a[h]["odds"]);o10=float(b[h]["odds"]);o5=float(x["odds"])
  wf1=_win_flow(o15,o10);wf2=_win_flow(o10,o5);wm=.30*wf1+.70*wf2
  p=profiles.get(int(h),{})
  raw.append({**x,"base15":a[h]["base"],"base10":b[h]["base"],"base5":x["base"],
   "d1":d1,"d2":d2,"agree":agree,"persist":persist,"odds15":o15,"odds10":o10,"odds5":o5,
   "win_flow1":wf1,"win_flow2":wf2,"win_move":wm,"profile":p})
 if not raw:return []
 wz=rz([r["win_move"] for r in raw]);out=[]
 for r,wzr in zip(raw,wz):
  level=math.tanh(max(0,r["base5"])/2);accel=math.tanh(max(0,r["d2"])/1.5)
  win_abs=math.tanh(max(0,r["win_move"])/0.30);win_rel=math.tanh(max(0,wzr)/2.0)
  win_signal=.45*win_abs+.55*win_rel
  win_persist=((1 if r["win_flow1"]>0 else 0)+(1 if r["win_flow2"]>0 else 0))/2
  persist2=.60*r["persist"]+.40*win_persist
  rel=float(r.get("reliability",tail_reliability(r["pop"],r["odds"])))
  rawh=100*(.30*level+.20*accel+.30*win_signal+.10*r["agree"]+.10*persist2)
  flow_score=max(0,min(100,round(rawh*rel)))
  flow_pct=((r["odds15"]/r["odds5"])-1.0)*100 if r["odds5"]>0 else 0.0
  p=r.get("profile") or {};style=p.get("style","不明");pace_fit=_pace_fit(style,front_count)
  form=p.get("form_score");dist=p.get("distance_score");course=p.get("course_score");jockey=p.get("jockey_score");cond=p.get("condition_score")
  edge,conf=_weighted_available([(form,.35),(dist,.20),(course,.15),(jockey,.10),(pace_fit,.10),(cond,.10)])
  out.append({**r,"persist":persist2,"reliability":rel,"flow_score":round(flow_score,1),"market_score":round(flow_score,1),
              "edge_score":round(edge,1) if edge is not None else None,"performance_score":round(edge,1) if edge is not None else None,
              "data_confidence":round(conf,1),"win_move_z":wzr,"win_move_score":round(100*win_signal),"win_flow_pct":flow_pct,
              "form_score":form,"distance_score":dist,"course_score":course,"jockey_score":jockey,
              "jockey_record":p.get("jockey_record",[0,0,0,0]),"jockey_has_data":bool(p.get("jockey_has_data",False)),
              "condition_score":cond,"pace_score":round(pace_fit,1) if pace_fit is not None else None,
              "style":style,"frame":p.get("frame"),"name":p.get("name") or f'{r["horse"]}番',"jockey":p.get("jockey",""),
              "recent_finishes":p.get("recent_finishes",[]),"body_weight":p.get("body_weight"),"body_diff":p.get("body_diff")})
 # VALUE = ability rank versus market popularity, with a small positive-flow confirmation.
 edge_rows=sorted([x for x in out if x.get("edge_score") is not None],key=lambda x:-x["edge_score"])
 edge_rank={x["horse"]:i+1 for i,x in enumerate(edge_rows)}
 for x in out:
  er=edge_rank.get(x["horse"]);edge=x.get("edge_score");flow=x.get("flow_score") or 0
  if er is None:value=None
  else:
   gap=float(x.get("pop") or er)-float(er)
   value=max(0.0,min(100.0,50.0+gap*5.0+max(0.0,flow-50.0)*.20-max(0.0,50.0-(edge or 50.0))*.20))
  x["value_score"]=round(value,1) if value is not None else None
  parts=[]
  if edge is not None:parts.append((edge,.65))
  parts.append((flow,.25 if edge is not None else .80))
  if value is not None:parts.append((value,.10 if edge is not None else .20))
  x["scope_score"]=round(sum(v*w for v,w in parts)/sum(w for _,w in parts),1)
  x["heuristic"]=round(flow,1)
 return out

def store_learning_samples(key,rs):
 rows=final_features(key)
 if not rows:return 0
 top=set(rs);now=datetime.now(JST).isoformat();c=con()
 for x in rows:
  c.execute("""INSERT INTO learning_samples
   (race_key,horse,label,base15,base10,base5,d1,d2,agree,persist,odds15,odds10,odds5,win_flow1,win_flow2,win_move,odds,pop,heuristic,created_at,parser_version)
   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON CONFLICT(race_key,horse) DO UPDATE SET label=EXCLUDED.label,base15=EXCLUDED.base15,base10=EXCLUDED.base10,base5=EXCLUDED.base5,d1=EXCLUDED.d1,d2=EXCLUDED.d2,agree=EXCLUDED.agree,persist=EXCLUDED.persist,odds15=EXCLUDED.odds15,odds10=EXCLUDED.odds10,odds5=EXCLUDED.odds5,win_flow1=EXCLUDED.win_flow1,win_flow2=EXCLUDED.win_flow2,win_move=EXCLUDED.win_move,odds=EXCLUDED.odds,pop=EXCLUDED.pop,heuristic=EXCLUDED.heuristic,created_at=EXCLUDED.created_at,parser_version=EXCLUDED.parser_version""",
   (key,x["horse"],1 if x["horse"] in top else 0,x["base15"],x["base10"],x["base5"],x["d1"],x["d2"],x["agree"],x["persist"],
    x["odds15"],x["odds10"],x["odds5"],x["win_flow1"],x["win_flow2"],x["win_move"],x["odds"],x["pop"],x["heuristic"],now,DATA_VERSION))
 c.commit();c.close();return len(rows)

def _vec(r):
 return [float(r["base15"]),float(r["base10"]),float(r["base5"]),float(r["d1"]),float(r["d2"]),
         float(r["agree"]),float(r["persist"]),float(r.get("win_flow1") or 0),float(r.get("win_flow2") or 0),float(r.get("win_move") or 0),
         math.log(max(float(r["odds"]),1.0001)),min(float(r["pop"]),18.0)/18.0]

def _fit(train):
 X=[_vec(r) for r in train];y=[int(r["label"]) for r in train];n=len(X);p=len(FEATURES)
 means=[sum(row[j] for row in X)/n for j in range(p)]
 stds=[]
 for j in range(p):
  v=sum((row[j]-means[j])**2 for row in X)/n
  stds.append(max(math.sqrt(v),1e-6))
 Z=[[(row[j]-means[j])/stds[j] for j in range(p)] for row in X]
 w=[0.0]*(p+1);lr=.06;l2=.002
 for epoch in range(900):
  g=[0.0]*(p+1)
  for row,t in zip(Z,y):
   pr=_sigmoid(w[0]+sum(w[j+1]*row[j] for j in range(p)))
   e=pr-t;g[0]+=e
   for j in range(p):g[j+1]+=e*row[j]
  for j in range(1,p+1):g[j]+=l2*n*w[j]
  step=lr/(1+epoch/350)
  for j in range(p+1):w[j]-=step*g[j]/n
 return w,means,stds

def _prob(r,w,means,stds):
 x=_vec(r);z=w[0]
 for j in range(len(FEATURES)):z+=w[j+1]*((x[j]-means[j])/stds[j])
 return _sigmoid(z)

def _top3_metric(rows,score_fn):
 by={}
 for r in rows:by.setdefault(r["race_key"],[]).append(r)
 hits=0;total=0
 for rr in by.values():
  pick=sorted(rr,key=score_fn,reverse=True)[:3]
  hits+=sum(int(x["label"]) for x in pick);total+=len(pick)
 return hits/total if total else 0.0

def maybe_train():
 c=con()
 rows=[dict(x) for x in c.execute("SELECT * FROM learning_samples WHERE COALESCE(parser_version,1)>=6 ORDER BY created_at,race_key,horse").fetchall()]
 races=[x["race_key"] for x in c.execute("SELECT race_key FROM results ORDER BY fetched_at").fetchall()
        if c.execute("SELECT 1 FROM learning_samples WHERE race_key=%s AND COALESCE(parser_version,1)>=6 LIMIT 1",(x["race_key"],)).fetchone()]
 state=dict(c.execute("SELECT * FROM model_state WHERE id=1").fetchone());c.close()
 uniq=[]
 for k in races:
  if k not in uniq:uniq.append(k)
 if len(uniq)<MIN_TRAIN_RACES or len(rows)<MIN_TRAIN_SAMPLES:
  c=con();c.execute("UPDATE model_state SET status=%s,trained_races=%s,trained_samples=%s,updated_at=%s WHERE id=1",
   ("COLLECTING",len(uniq),len(rows),datetime.now(JST).isoformat()));c.commit();c.close()
  return {"status":"COLLECTING","races":len(uniq),"samples":len(rows)}
 cut=max(1,int(len(uniq)*.75));train_keys=set(uniq[:cut]);val_keys=set(uniq[cut:])
 train=[r for r in rows if r["race_key"] in train_keys];val=[r for r in rows if r["race_key"] in val_keys]
 if len(val)<12:return {"status":"COLLECTING","races":len(uniq),"samples":len(rows)}
 w,mn,sd=_fit(train)
 h=_top3_metric(val,lambda r:float(r["heuristic"]))
 l=_top3_metric(val,lambda r:_prob(r,w,mn,sd))
 oldv=int(state.get("version") or 0)
 # Adopt only when learned ranking beats heuristic on chronological holdout.
 if l>h:
  version=oldv+1
  c=con();c.execute("""UPDATE model_state SET version=%s,status='ACTIVE',trained_races=%s,trained_samples=%s,
   weights=%s,means=%s,stds=%s,heuristic_val=%s,learned_val=%s,updated_at=%s WHERE id=1""",
   (version,len(uniq),len(rows),json.dumps(w),json.dumps(mn),json.dumps(sd),h,l,datetime.now(JST).isoformat()))
  c.commit();c.close();return {"status":"ACTIVE","version":version,"heuristic_val":h,"learned_val":l}
 c=con();c.execute("""UPDATE model_state SET status=%s,trained_races=%s,trained_samples=%s,heuristic_val=%s,learned_val=%s,updated_at=%s WHERE id=1""",
  ("KEEP_HEURISTIC",len(uniq),len(rows),h,l,datetime.now(JST).isoformat()));c.commit();c.close()
 return {"status":"KEEP_HEURISTIC","version":oldv,"heuristic_val":h,"learned_val":l}

def get_model_state():
 try:
  c=con();row=c.execute("SELECT * FROM model_state WHERE id=1").fetchone();c.close()
  return dict(row) if row else {}
 except Exception:
  return {}

def model_score(x,state=None):
 s=state if state is not None else get_model_state()
 if s.get("status")!="ACTIVE" or not s.get("weights"):return None
 try:
  r={"base15":x["base15"],"base10":x["base10"],"base5":x["base5"],"d1":x["d1"],"d2":x["d2"],
     "agree":x["agree"],"persist":x["persist"],"win_flow1":x.get("win_flow1",0),"win_flow2":x.get("win_flow2",0),"win_move":x.get("win_move",0),
     "odds":x["odds"],"pop":x["pop"]}
  return _prob(r,json.loads(s["weights"]),json.loads(s["means"]),json.loads(s["stds"]))
 except:return None


def parse_result(s):
 rows=[]
 for tr in s.find_all("tr"):
  cells=[" ".join(x.stripped_strings) for x in tr.find_all(["td","th"])];nums=[int(x) for x in cells if re.fullmatch(r"\d{1,2}",x or "")]
  if len(nums)>=3 and nums[0] in (1,2,3) and 1<=nums[2]<=18:rows.append((nums[0],nums[2]))
 d=dict(rows);return [d.get(1),d.get(2),d.get(3)]
def due():
 now=datetime.now(JST);c=con();rr=[dict(x) for x in c.execute("SELECT * FROM races WHERE status='reserved'").fetchall()];c.close();ev=[]
 profile_budget=2  # avoid making one cron tick wait on every future race at once
 for r in rr:
  mins=(datetime.fromisoformat(r["start_iso"])-now).total_seconds()/60
  if -5<=mins<=360 and profile_budget>0:
   try:
    if not load_profiles(r["race_key"]):
     get_profiles(r);profile_budget-=1;ev.append(f'{r["race_key"]}:PROFILE')
   except Exception as e:ev.append("ERR profile "+str(e))
  for slot in (15,10,5):
   c=con();oldrow=c.execute("SELECT payload FROM snapshots WHERE race_key=%s AND slot=%s",(r["race_key"],slot)).fetchone();c.close()
   ex=False
   if oldrow:
    try:ex=valid_payload(json.loads(oldrow["payload"]))
    except:ex=False
   if not ex and slot-2<=mins<=slot+1:
    try:
     data=take(r);c=con();c.execute("""INSERT INTO snapshots VALUES(%s,%s,%s,%s)
      ON CONFLICT(race_key,slot) DO UPDATE SET fetched_at=EXCLUDED.fetched_at,payload=EXCLUDED.payload""",
      (r["race_key"],slot,data["fetched_at"],json.dumps(data,ensure_ascii=False)));c.commit();c.close();ev.append(f'{r["race_key"]}:{slot}')
     if slot==5:save_predictions(r["race_key"])
    except Exception as e:ev.append("ERR snapshot "+str(e))
  if mins < -3 and not r["result_checked"]:
   try:
    rs=parse_result(soup("RaceMarkTable",qfor(r)))
    if all(rs):
     c=con();c.execute("INSERT INTO results VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(race_key) DO UPDATE SET first_horse=EXCLUDED.first_horse,second_horse=EXCLUDED.second_horse,third_horse=EXCLUDED.third_horse,fetched_at=EXCLUDED.fetched_at,payload=EXCLUDED.payload",(r["race_key"],*rs,now.isoformat(),json.dumps(rs)));c.execute("UPDATE races SET result_checked=1,status='complete' WHERE race_key=%s",(r["race_key"],));c.commit();c.close()
     store_learning_samples(r["race_key"],rs);maybe_train();ev.append(f'{r["race_key"]}:RESULT:{rs}')
   except Exception as e:ev.append("ERR result "+str(e))
 return ev

@app.route("/")
def home():return send_from_directory(".","index.html")
@app.route("/api/reserve",methods=["POST"])
def reserve():
 try:
  x=request.get_json(force=True);key=f'{x["date"]}:{x["baba"]}:{int(x["race"])}';st=datetime.fromisoformat(x["start_iso"])
  if (st-datetime.now(JST)).total_seconds()<16*60:return jsonify(ok=False,error="15・10・5分前の3時点取得に必要なため、発走16分前までに予約してください"),409
  c=con()
  c.execute("""INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0)
  ON CONFLICT(race_key) DO UPDATE SET start_iso=excluded.start_iso,baba_name=excluded.baba_name,status='reserved',result_checked=0""",(key,x["date"],str(x["baba"]),x["baba_name"],int(x["race"]),st.isoformat(),"reserved",datetime.now(JST).isoformat()))
  c.commit();c.close()
  # Return immediately. Official entry-table profiles are prefetched by /api/tick so reservation does not block on NAR.
  return jsonify(ok=True,race_key=key,profile_status="queued")
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/races")
def races():
 # Queue endpoint must stay lightweight. Full hybrid ranking is computed only
 # when the user opens Detail (/api/status). This prevents one queue refresh
 # from opening hundreds of Supabase connections across historical races.
 try:
  c=con();rr=[dict(x) for x in c.execute("SELECT * FROM races WHERE status!='cancelled' ORDER BY start_iso DESC LIMIT 50").fetchall()]
  out=[]
  for r in rr:
   try:
    ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(r["race_key"],)).fetchall()
    S={}
    for x in ss:
     try:
      p=json.loads(x["payload"]);
      if valid_payload(p):S[str(x["slot"])]=p
     except Exception:
      pass
    stages={}
    if "15" in S:stages["15"]=point_signals(S["15"])
    if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
    if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
    rs=c.execute("SELECT * FROM results WHERE race_key=%s",(r["race_key"],)).fetchone();result=dict(rs) if rs else None
    out.append({**r,"slots":[int(x) for x in S],"stages":stages,"result":result})
   except Exception as e:
    # One malformed historical race must never take down the entire queue.
    out.append({**r,"slots":[],"stages":{},"result":None,"queue_warning":str(e)[:160]})
  c.close();return jsonify(races=out)
 except Exception as e:
  try:c.close()
  except Exception:pass
  return jsonify(races=[],error=str(e)),200
@app.route("/api/cancel",methods=["POST"])
def cancel():
 try:
  key=request.get_json(force=True)["race_key"];c=con();c.execute("UPDATE races SET status='cancelled' WHERE race_key=%s",(key,));c.commit();c.close();return jsonify(ok=True)
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/tick",methods=["GET","POST"])
def tick():
 try:return jsonify(ok=True,events=due(),at=datetime.now(JST).isoformat())
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/status")
def status():
 try:
  key=request.args["race_key"]
  c=con();r=c.execute("SELECT * FROM races WHERE race_key=%s",(key,)).fetchone()
  ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(key,)).fetchall()
  pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=%s ORDER BY rank",(key,)).fetchall()]
  rs=c.execute("SELECT * FROM results WHERE race_key=%s",(key,)).fetchone();c.close()
  S={}
  for x in ss:
   try:
    p=json.loads(x["payload"]);
    if valid_payload(p):S[str(x["slot"])]=p
   except Exception:
    pass
  stages={}
  if "15" in S:stages["15"]=point_signals(S["15"])
  if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
  if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
  full=all(k in S for k in ("15","10","5"))
  features=final_features(key) if full else []
  fm={x["horse"]:x for x in features}
  for p in pp:
   if p["horse"] in fm:p.update({k:fm[p["horse"]].get(k) for k in ("odds15","odds10","odds5","win_flow1","win_flow2","win_move","win_move_score","win_flow_pct","market_score","flow_score","performance_score","edge_score","value_score","scope_score","data_confidence","form_score","distance_score","course_score","jockey_score","jockey_record","jockey_has_data","condition_score","pace_score","style","frame","name","jockey","recent_finishes","body_weight","body_diff")})
   p["strength"]=signal_strength(p)
  result=dict(rs) if rs else None
  if result:
   places={result["first_horse"]:1,result["second_horse"]:2,result["third_horse"]:3}
   for p in pp:p["finish"]=places.get(p["horse"],0)
  state=get_model_state() if full else {}
  rankings=rank_features(features,state) if full else []
  profile_ready=any(x.get("name") and x.get("frame") for x in features) if full else bool(load_profiles(key))
  display_predictions=[]
  if rankings:
   selected=[]
   if rankings:selected.append(rankings[0])
   if len(rankings)>1:selected.append(rankings[1])
   mid=next((x for x in rankings if 4<=int(x.get("pop") or 99)<=9 and x not in selected),None)
   if mid:selected.append(mid)
   elif len(rankings)>2:selected.append(next((x for x in rankings if x not in selected),rankings[2]))
   for i,x in enumerate(selected[:3],1):
    y=dict(x);y["rank"]=i;y["strength"]=scope_label(y);display_predictions.append(y)
  return jsonify(race=dict(r) if r else None,snaps=S,stages=stages,predictions=display_predictions,rankings=rankings,result=result,profile_ready=profile_ready)
 except Exception as e:
  try:c.close()
  except Exception:pass
  return jsonify(ok=False,error=str(e)),500
@app.route("/api/repair-result",methods=["POST"])
def repair_result():
 try:
  key=request.get_json(force=True)["race_key"];c=con();r=c.execute("SELECT * FROM races WHERE race_key=%s",(key,)).fetchone();c.close()
  if not r:return jsonify(ok=False,error="race not found"),404
  rs=parse_result(soup("RaceMarkTable",qfor(r)))
  if not all(rs):return jsonify(ok=False,error="official result not available"),409
  now=datetime.now(JST);c=con();c.execute("INSERT INTO results VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(race_key) DO UPDATE SET first_horse=EXCLUDED.first_horse,second_horse=EXCLUDED.second_horse,third_horse=EXCLUDED.third_horse,fetched_at=EXCLUDED.fetched_at,payload=EXCLUDED.payload",(key,*rs,now.isoformat(),json.dumps(rs)));c.execute("UPDATE races SET result_checked=1,status='complete' WHERE race_key=%s",(key,));c.commit();c.close()
  store_learning_samples(key,rs);train=maybe_train()
  return jsonify(ok=True,result=rs,training=train)
 except Exception as e:return jsonify(ok=False,error=str(e)),500

@app.route("/api/odds-check")
def odds_check():
 try:
  q={"k_babaCode":request.args["baba"],"k_raceDate":request.args["date"].replace("-","/"),"k_raceNo":int(request.args["race"])}
  W=win(soup("OddsTanFuku",q));market_sum=sum(1.0/o for _,o in W if o>0)
  return jsonify(ok=bool(W) and 0.55<=market_sum<=2.20,count=len(W),market_sum=round(market_sum,4),win_odds=W)
 except Exception as e:return jsonify(ok=False,error=str(e)),500


@app.route("/api/profile-refresh",methods=["POST"])
def profile_refresh():
 try:
  key=request.get_json(force=True)["race_key"];c=con();r=c.execute("SELECT * FROM races WHERE race_key=%s",(key,)).fetchone();c.close()
  if not r:return jsonify(ok=False,error="race not found"),404
  p=get_profiles(dict(r),force=True);return jsonify(ok=True,count=len(p),profiles=p)
 except Exception as e:return jsonify(ok=False,error=str(e)),500


@app.route("/api/learning")
def learning():
 c=con()
 samples=c.execute("SELECT COUNT(*) n FROM learning_samples WHERE COALESCE(parser_version,1)>=6").fetchone()["n"]
 lraces=c.execute("SELECT COUNT(DISTINCT race_key) n FROM learning_samples WHERE COALESCE(parser_version,1)>=6").fetchone()["n"]
 n=lraces
 p=c.execute("""SELECT p.*,r.first_horse,r.second_horse,r.third_horse FROM predictions p JOIN results r USING(race_key)
              WHERE EXISTS(SELECT 1 FROM learning_samples l WHERE l.race_key=p.race_key AND COALESCE(l.parser_version,1)>=4)""").fetchall()
 s=dict(c.execute("SELECT * FROM model_state WHERE id=1").fetchone());c.close()
 total=len(p);hit=sum(x["horse"] in (x["first_horse"],x["second_horse"],x["third_horse"]) for x in p)
 by={}
 for x in p:by.setdefault(x["race_key"],[]).append(x)
 signal_races=len(by);race_hit=sum(any(x["horse"] in (x["first_horse"],x["second_horse"],x["third_horse"]) for x in rr) for rr in by.values())
 return jsonify(completed_races=n,predictions=total,top3_hits=hit,top3_rate=(hit/total if total else None),signal_races=signal_races,race_hit_rate=(race_hit/signal_races if signal_races else None),
  learning_races=lraces,learning_samples=samples,model_status=s["status"],model_version=s["version"],
  min_train_races=MIN_TRAIN_RACES,min_train_samples=MIN_TRAIN_SAMPLES,
  heuristic_val=s["heuristic_val"],learned_val=s["learned_val"],updated_at=s["updated_at"])
