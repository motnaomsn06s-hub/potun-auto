from flask import Flask,request,jsonify,send_from_directory
import requests,re,math,os,json
import psycopg
from psycopg.rows import dict_row
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta

app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0 (compatible; OddsSignal/0.9.5)"}
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
 """
 for stmt in schema.split(";"):
  if stmt.strip(): c.execute(stmt)
 c.execute("INSERT INTO model_state(id,status) VALUES(1,'COLLECTING') ON CONFLICT(id) DO NOTHING")
 c.commit();c.close()
 # PostgreSQL migration. Version 5 keeps direct money-flow features and recalibrates candidate gating.
 c=con()
 for col,typ in (("parser_version","INTEGER DEFAULT 1"),("odds15","DOUBLE PRECISION"),("odds10","DOUBLE PRECISION"),("odds5","DOUBLE PRECISION"),
                 ("win_flow1","DOUBLE PRECISION"),("win_flow2","DOUBLE PRECISION"),("win_move","DOUBLE PRECISION")):
  try:
   c.execute(f"ALTER TABLE learning_samples ADD COLUMN {col} {typ}");c.commit()
  except Exception:c.rollback()
 c.close()
 c=con();done=c.execute("SELECT v FROM app_meta WHERE k='analysis_v5_adaptive_gate'").fetchone()
 if not done:
  c.execute("UPDATE model_state SET status='COLLECTING',trained_races=0,trained_samples=0,weights=NULL,means=NULL,stds=NULL,heuristic_val=NULL,learned_val=NULL,updated_at=%s WHERE id=1",(datetime.now(JST).isoformat(),))
  c.execute("INSERT INTO app_meta(k,v) VALUES('analysis_v5_adaptive_gate','1') ON CONFLICT(k) DO UPDATE SET v='1'")
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
DATA_VERSION=5


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

def predictions(key):
 A=final_features(key);out=[]
 for x in A:
  lp=model_score(x)
  # Keep heuristic score visible; learned probability is separate.
  # If a validated learned model is active, use it only for ranking.
  x["score"]=x["heuristic"];x["learned_prob"]=lp
  x["rank_score"]=(lp if lp is not None else x["heuristic"]/100.0)
  out.append(x)
 return sorted(out,key=lambda x:-x["rank_score"])
def adaptive_candidates(A):
 if not A:return [],None
 scores=[float(x.get("score") or 0) for x in A]
 m=med(scores);mad=med([abs(v-m) for v in scores])
 # Absolute floor prevents noise; race-relative cutoff prevents the v9.4 scale
 # from suppressing every race simply because all heuristic scores are compressed.
 relative=m+(0.75*1.4826*mad if mad>1e-9 else 0.0)
 cutoff=max(28.0,min(48.0,relative))
 C=[]
 for x in A:
  sc=float(x.get("score") or 0);wm=float(x.get("win_move_score") or 0);flow=float(x.get("win_flow_pct") or 0)
  d2=float(x.get("d2") or 0);agree=float(x.get("agree") or 0);persist=float(x.get("persist") or 0)
  # Need at least one direct money-flow/anomaly acceleration signal and one corroborating signal.
  movement=(wm>=10 or flow>=8 or d2>0.08)
  corroborated=(agree>=1/3 or persist>=0.50)
  if sc>=cutoff and movement and corroborated:C.append(x)
 # Borderline fallback: allow one WATCH candidate only when direct win-odds flow is genuinely visible.
 if not C and A:
  x=A[0];sc=float(x.get("score") or 0);wm=float(x.get("win_move_score") or 0);flow=float(x.get("win_flow_pct") or 0)
  agree=float(x.get("agree") or 0);persist=float(x.get("persist") or 0)
  if sc>=25 and (wm>=18 or flow>=12) and (agree>=1/3 or persist>=0.50):C=[x]
 return C[:3],round(cutoff,1)

def save_predictions(key):
 A=predictions(key);C,_=adaptive_candidates(A)
 c=con();c.execute("DELETE FROM predictions WHERE race_key=%s",(key,))
 for i,x in enumerate(C,1):c.execute("INSERT INTO predictions VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(race_key,horse) DO UPDATE SET rank=EXCLUDED.rank,score=EXCLUDED.score,pop=EXCLUDED.pop,odds=EXCLUDED.odds,d1=EXCLUDED.d1,d2=EXCLUDED.d2,agree=EXCLUDED.agree,created_at=EXCLUDED.created_at",(key,x["horse"],i,x["score"],x["pop"],x["odds"],x["d1"],x["d2"],x["agree"],datetime.now(JST).isoformat()))
 c.commit();c.close();return C
def signal_strength(x):
 rel=tail_reliability(x.get("pop",99),x.get("odds",999))
 wm=float(x.get("win_move_score") or 0);flow=float(x.get("win_flow_pct") or 0);d2=float(x.get("d2") or 0)
 if x["score"]>=68 and x.get("agree",0)>=2/3 and (wm>=30 or d2>0.20) and rel>=.80:return "STRONG"
 if x["score"]>=45 and x.get("agree",0)>=1/3 and (wm>=12 or flow>=8 or d2>0.08):return "MEDIUM"
 if x["score"]>=25 and (wm>=10 or flow>=8 or d2>0.05):return "WATCH"
 return "WEAK"

FEATURES=("base15","base10","base5","d1","d2","agree","persist","win_flow1","win_flow2","win_move","log_odds","pop_scaled")
MIN_TRAIN_RACES=30
MIN_TRAIN_SAMPLES=200

def _sigmoid(z):
 z=max(-35.0,min(35.0,z))
 return 1.0/(1.0+math.exp(-z))

def final_features(key):
 c=con();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(key,)).fetchall();c.close()
 S={str(x["slot"]):json.loads(x["payload"]) for x in ss};S={k:v for k,v in S.items() if valid_payload(v)}
 if not all(k in S for k in ("15","10","5")):return []
 def mp(k):return {str(x["horse"]):x for x in S[k].get("rows",[])}
 a,b,z=mp("15"),mp("10"),mp("5");raw=[]
 for h,x in z.items():
  if h not in a or h not in b:continue
  d1=b[h]["base"]-a[h]["base"];d2=x["base"]-b[h]["base"]
  persist=sum(v["base"]>0 for v in (a[h],b[h],x))/3
  agree=sum(v>0 for v in (x["Q"],x["E"],x["T"]))/3
  o15=float(a[h]["odds"]);o10=float(b[h]["odds"]);o5=float(x["odds"])
  wf1=_win_flow(o15,o10);wf2=_win_flow(o10,o5);wm=.30*wf1+.70*wf2
  raw.append({**x,"base15":a[h]["base"],"base10":b[h]["base"],"base5":x["base"],
   "d1":d1,"d2":d2,"agree":agree,"persist":persist,"odds15":o15,"odds10":o10,"odds5":o5,
   "win_flow1":wf1,"win_flow2":wf2,"win_move":wm})
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
  heuristic=max(0,min(100,round(rawh*rel)))
  flow_pct=((r["odds15"]/r["odds5"])-1.0)*100 if r["odds5"]>0 else 0.0
  out.append({**r,"persist":persist2,"reliability":rel,"heuristic":heuristic,"win_move_z":wzr,
              "win_move_score":round(100*win_signal),"win_flow_pct":flow_pct})
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
 rows=[dict(x) for x in c.execute("SELECT * FROM learning_samples WHERE COALESCE(parser_version,1)>=5 ORDER BY created_at,race_key,horse").fetchall()]
 races=[x["race_key"] for x in c.execute("SELECT race_key FROM results ORDER BY fetched_at").fetchall()
        if c.execute("SELECT 1 FROM learning_samples WHERE race_key=%s AND COALESCE(parser_version,1)>=5 LIMIT 1",(x["race_key"],)).fetchone()]
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

def model_score(x):
 c=con();s=dict(c.execute("SELECT * FROM model_state WHERE id=1").fetchone());c.close()
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
 for r in rr:
  mins=(datetime.fromisoformat(r["start_iso"])-now).total_seconds()/60
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
  c.commit();c.close();return jsonify(ok=True,race_key=key)
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/races")
def races():
 c=con();rr=[dict(x) for x in c.execute("SELECT * FROM races WHERE status!='cancelled' ORDER BY start_iso DESC LIMIT 50").fetchall()];out=[]
 for r in rr:
  ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(r["race_key"],)).fetchall();S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
  S={k:v for k,v in S.items() if valid_payload(v)}
  stages={}
  if "15" in S:stages["15"]=point_signals(S["15"])
  if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
  if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
  pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=%s ORDER BY rank",(r["race_key"],)).fetchall()] if all(k in S for k in ("15","10","5")) else []
  fm={x["horse"]:x for x in final_features(r["race_key"])} if all(k in S for k in ("15","10","5")) else {}
  for p in pp:
   if p["horse"] in fm:p.update({k:fm[p["horse"]].get(k) for k in ("odds15","odds10","odds5","win_flow1","win_flow2","win_move","win_move_score","win_flow_pct")})
   p["strength"]=signal_strength(p)
  rs=c.execute("SELECT * FROM results WHERE race_key=%s",(r["race_key"],)).fetchone();result=dict(rs) if rs else None
  if result:
   places={result["first_horse"]:1,result["second_horse"]:2,result["third_horse"]:3}
   for p in pp:p["finish"]=places.get(p["horse"],0)
  out.append({**r,"slots":[int(x) for x in S],"stages":stages,"predictions":pp,"result":result})
 c.close();return jsonify(races=out)
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
 key=request.args["race_key"];c=con();r=c.execute("SELECT * FROM races WHERE race_key=%s",(key,)).fetchone();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(key,)).fetchall();S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
 S={k:v for k,v in S.items() if valid_payload(v)}
 stages={}
 if "15" in S:stages["15"]=point_signals(S["15"])
 if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
 if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
 pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=%s ORDER BY rank",(key,)).fetchall()] if all(k in S for k in ("15","10","5")) else []
 fm={x["horse"]:x for x in final_features(key)} if all(k in S for k in ("15","10","5")) else {}
 for p in pp:
  if p["horse"] in fm:p.update({k:fm[p["horse"]].get(k) for k in ("odds15","odds10","odds5","win_flow1","win_flow2","win_move","win_move_score","win_flow_pct")})
  p["strength"]=signal_strength(p)
 rs=c.execute("SELECT * FROM results WHERE race_key=%s",(key,)).fetchone();result=dict(rs) if rs else None
 if result:
  places={result["first_horse"]:1,result["second_horse"]:2,result["third_horse"]:3}
  for p in pp:p["finish"]=places.get(p["horse"],0)
 c.close()
 return jsonify(race=dict(r) if r else None,snaps=S,stages=stages,predictions=pp,result=result)
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

@app.route("/api/learning")
def learning():
 c=con()
 samples=c.execute("SELECT COUNT(*) n FROM learning_samples WHERE COALESCE(parser_version,1)>=5").fetchone()["n"]
 lraces=c.execute("SELECT COUNT(DISTINCT race_key) n FROM learning_samples WHERE COALESCE(parser_version,1)>=5").fetchone()["n"]
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
