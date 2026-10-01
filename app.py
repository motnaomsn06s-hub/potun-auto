from flask import Flask,request,jsonify,send_from_directory
import requests,re,math,os,json
import psycopg
from psycopg.rows import dict_row
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta

app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0 (compatible; OddsSignal/0.8)"}
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
   odds DOUBLE PRECISION,pop INTEGER,heuristic DOUBLE PRECISION,created_at TEXT,
   PRIMARY KEY(race_key,horse)
 );
 CREATE TABLE IF NOT EXISTS model_state(
   id INTEGER PRIMARY KEY CHECK(id=1),version INTEGER DEFAULT 0,status TEXT DEFAULT 'COLLECTING',
   trained_races INTEGER DEFAULT 0,trained_samples INTEGER DEFAULT 0,
   weights TEXT,means TEXT,stds TEXT,
   heuristic_val DOUBLE PRECISION,learned_val DOUBLE PRECISION,updated_at TEXT
 );
 """
 for stmt in schema.split(";"):
  if stmt.strip(): c.execute(stmt)
 c.execute("INSERT INTO model_state(id,status) VALUES(1,'COLLECTING') ON CONFLICT(id) DO NOTHING")
 c.commit();c.close()
init()

def soup(path,q):
 r=requests.get(BASE+path,params=q,headers=UA,timeout=20);r.raise_for_status();r.encoding=r.apparent_encoding or r.encoding
 return BeautifulSoup(r.text,"html.parser")
def qfor(r):return {"k_babaCode":r["baba"],"k_raceDate":r["date"].replace("-","/"),"k_raceNo":r["race"]}
def win(s):
 # NAR OddsTanFuku table starts with: 人気 / 馬番 / 印 / 馬名 / 単勝オッズ / 複勝オッズ.
 # The old parser incorrectly treated the first integer (人気) as 馬番.
 out={}
 for tr in s.find_all("tr"):
  cells=[" ".join(x.stripped_strings) for x in tr.find_all(["td","th"])]
  if len(cells)<5:continue
  if not re.fullmatch(r"\d{1,2}",cells[0] or ""):continue
  if not re.fullmatch(r"\d{1,2}",cells[1] or ""):continue
  h=int(cells[1])
  if not 1<=h<=18:continue
  o=None
  # Prefer the single-win-odds column after horse name.
  for z in cells[4:]:
   m=re.search(r"(%s<!\d)(\d+(%s:\.\d+)%s)(%s!\d)",z.replace(",",""))
   if m:
    v=float(m.group(1))
    if v>=1:
     o=v;break
  if o is not None:out[h]=o
 return [[h,o] for h,o in out.items()]
def combo(s,n):
 t=s.get_text(" ",strip=True).replace("→","-").replace("－","-")
 pat=r"(%s<!\d)(\d{1,2})\s*-\s*(\d{1,2})"+(r"\s*-\s*(\d{1,2})" if n==3 else "")+r"\s+([\d,.]+)"
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
def agg(a):
 a=sorted([x for x in a if x>0],reverse=True);return .5*(a[0] if a else 0)+.3*(a[1] if len(a)>1 else 0)+.2*(a[2] if len(a)>2 else 0)
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
  ms=sum(mod);aa=sum(act);D=[math.log(max(a/aa,1e-15)/max(m/ms,1e-15)) for a,m in zip(act,mod)]
  for z,hs in zip(rz(D),hh):
   for h in hs:
    if h in B:B[h][k].append(z)
 market(Q,"Q");market(E,"E");market(T,"T")
 return [{"horse":h,"odds":od[h],"pop":pop[h],"Q":agg(b["Q"]),"E":agg(b["E"]),"T":agg(b["T"]),"base":(agg(b["Q"])+agg(b["E"])+agg(b["T"]))/3} for h,b in B.items()]
def take(r):
 q=qfor(r);W=win(soup("OddsTanFuku",q));Q=combo(soup("OddsUmLenFuku",q),2);E=combo(soup("OddsUmLenTan",q),2);T=combo(soup("Odds3LenTan",q),3)
 cnt={"win":len(W),"Q":len(Q),"E":len(E),"T":len(T)}
 if len(W)<3 or min(len(Q),len(E),len(T))==0:raise RuntimeError("オッズ取得不完全 "+str(cnt))
 return {"rows":analyse(W,Q,E,T),"counts":cnt,"fetched_at":datetime.now(JST).isoformat(timespec="seconds")}

def point_signals(payload,prev=None):
 rows=payload.get("rows",[]);pm={str(x["horse"]):x for x in (prev or {}).get("rows",[])}
 raw=[]
 for x in rows:
  agree=sum(v>0 for v in (x["Q"],x["E"],x["T"]))/3
  level=math.tanh(max(0,x["base"])/2)
  delta=x["base"]-pm.get(str(x["horse"]),x)["base"] if pm else 0
  move=math.tanh(max(0,delta)/1.5) if pm else 0
  score=round(100*((.72 if not pm else .52)*level+(.0 if not pm else .28)*move+.20*agree))
  raw.append({**x,"delta":delta,"agree":agree,"score":max(0,min(100,score))})
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
def save_predictions(key):
 A=predictions(key);C=A[:3];c=con()
 c.execute("DELETE FROM predictions WHERE race_key=%s",(key,))
 for i,x in enumerate(C,1):c.execute("INSERT INTO predictions VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(race_key,horse) DO UPDATE SET rank=EXCLUDED.rank,score=EXCLUDED.score,pop=EXCLUDED.pop,odds=EXCLUDED.odds,d1=EXCLUDED.d1,d2=EXCLUDED.d2,agree=EXCLUDED.agree,created_at=EXCLUDED.created_at",(key,x["horse"],i,x["score"],x["pop"],x["odds"],x["d1"],x["d2"],x["agree"],datetime.now(JST).isoformat()))
 c.commit();c.close();return C
def signal_strength(x):
 if x["score"]>=75 and x["agree"]>=2/3 and x["d2"]>0:return "STRONG"
 if x["score"]>=55 and x["agree"]>=1/3:return "MEDIUM"
 return "WEAK"


FEATURES=("base15","base10","base5","d1","d2","agree","persist","log_odds","pop_scaled")
MIN_TRAIN_RACES=30
MIN_TRAIN_SAMPLES=200

def _sigmoid(z):
 z=max(-35.0,min(35.0,z))
 return 1.0/(1.0+math.exp(-z))

def final_features(key):
 c=con();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(key,)).fetchall();c.close()
 S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
 if not all(k in S for k in ("15","10","5")):return []
 def mp(k):return {str(x["horse"]):x for x in S[k].get("rows",[])}
 a,b,z=mp("15"),mp("10"),mp("5");out=[]
 for h,x in z.items():
  if h not in a or h not in b:continue
  d1=b[h]["base"]-a[h]["base"];d2=x["base"]-b[h]["base"]
  persist=sum(v["base"]>0 for v in (a[h],b[h],x))/3
  agree=sum(v>0 for v in (x["Q"],x["E"],x["T"]))/3
  level=math.tanh(max(0,x["base"])/2);accel=math.tanh(max(0,d2)/1.5)
  heuristic=max(0,min(100,round(100*(.40*level+.30*accel+.15*persist+.15*agree))))
  out.append({
   **x,"base15":a[h]["base"],"base10":b[h]["base"],"base5":x["base"],
   "d1":d1,"d2":d2,"agree":agree,"persist":persist,"heuristic":heuristic
  })
 return out

def store_learning_samples(key,rs):
 rows=final_features(key)
 if not rows:return 0
 top=set(rs);now=datetime.now(JST).isoformat();c=con()
 for x in rows:
  c.execute("""INSERT INTO learning_samples
   (race_key,horse,label,base15,base10,base5,d1,d2,agree,persist,odds,pop,heuristic,created_at)
   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON CONFLICT(race_key,horse) DO UPDATE SET label=EXCLUDED.label,base15=EXCLUDED.base15,base10=EXCLUDED.base10,base5=EXCLUDED.base5,d1=EXCLUDED.d1,d2=EXCLUDED.d2,agree=EXCLUDED.agree,persist=EXCLUDED.persist,odds=EXCLUDED.odds,pop=EXCLUDED.pop,heuristic=EXCLUDED.heuristic,created_at=EXCLUDED.created_at""",
   (key,x["horse"],1 if x["horse"] in top else 0,x["base15"],x["base10"],x["base5"],
    x["d1"],x["d2"],x["agree"],x["persist"],x["odds"],x["pop"],x["heuristic"],now))
 c.commit();c.close();return len(rows)

def _vec(r):
 return [float(r["base15"]),float(r["base10"]),float(r["base5"]),float(r["d1"]),float(r["d2"]),
         float(r["agree"]),float(r["persist"]),math.log(max(float(r["odds"]),1.0001)),
         min(float(r["pop"]),18.0)/18.0]

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
 rows=[dict(x) for x in c.execute("SELECT * FROM learning_samples ORDER BY created_at,race_key,horse").fetchall()]
 races=[x["race_key"] for x in c.execute("SELECT race_key FROM results ORDER BY fetched_at").fetchall()
        if c.execute("SELECT 1 FROM learning_samples WHERE race_key=%s LIMIT 1",(x["race_key"],)).fetchone()]
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
     "agree":x["agree"],"persist":x["persist"],"odds":x["odds"],"pop":x["pop"]}
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
   c=con();ex=c.execute("SELECT 1 FROM snapshots WHERE race_key=%s AND slot=%s",(r["race_key"],slot)).fetchone();c.close()
   if not ex and slot-2<=mins<=slot+1:
    try:
     data=take(r);c=con();c.execute("INSERT INTO snapshots VALUES(%s,%s,%s,%s) ON CONFLICT(race_key,slot) DO NOTHING",(r["race_key"],slot,data["fetched_at"],json.dumps(data,ensure_ascii=False)));c.commit();c.close();ev.append(f'{r["race_key"]}:{slot}')
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
  x=request.get_json(force=True);key=f'{x["date"]}:{x["baba"]}:{int(x["race"])}';st=datetime.fromisoformat(x["start_iso"]);c=con()
  c.execute("""INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0)
  ON CONFLICT(race_key) DO UPDATE SET start_iso=excluded.start_iso,baba_name=excluded.baba_name,status='reserved',result_checked=0""",(key,x["date"],str(x["baba"]),x["baba_name"],int(x["race"]),st.isoformat(),"reserved",datetime.now(JST).isoformat()))
  c.commit();c.close();return jsonify(ok=True,race_key=key)
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/races")
def races():
 c=con();rr=[dict(x) for x in c.execute("SELECT * FROM races WHERE status!='cancelled' ORDER BY start_iso DESC LIMIT 50").fetchall()];out=[]
 for r in rr:
  ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(r["race_key"],)).fetchall();S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
  stages={}
  if "15" in S:stages["15"]=point_signals(S["15"])
  if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
  if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
  pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=%s ORDER BY rank",(r["race_key"],)).fetchall()]
  for p in pp:p["strength"]=signal_strength(p)
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
 stages={}
 if "15" in S:stages["15"]=point_signals(S["15"])
 if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
 if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
 pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=%s ORDER BY rank",(key,)).fetchall()]
 for p in pp:p["strength"]=signal_strength(p)
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

@app.route("/api/learning")
def learning():
 c=con()
 n=c.execute("SELECT COUNT(*) n FROM results").fetchone()["n"]
 p=c.execute("SELECT p.*,r.first_horse,r.second_horse,r.third_horse FROM predictions p JOIN results r USING(race_key)").fetchall()
 samples=c.execute("SELECT COUNT(*) n FROM learning_samples").fetchone()["n"]
 lraces=c.execute("SELECT COUNT(DISTINCT race_key) n FROM learning_samples").fetchone()["n"]
 s=dict(c.execute("SELECT * FROM model_state WHERE id=1").fetchone());c.close()
 total=len(p);hit=sum(x["horse"] in (x["first_horse"],x["second_horse"],x["third_horse"]) for x in p)
 return jsonify(completed_races=n,predictions=total,top3_hits=hit,top3_rate=(hit/total if total else None),
  learning_races=lraces,learning_samples=samples,model_status=s["status"],model_version=s["version"],
  min_train_races=MIN_TRAIN_RACES,min_train_samples=MIN_TRAIN_SAMPLES,
  heuristic_val=s["heuristic_val"],learned_val=s["learned_val"],updated_at=s["updated_at"])
