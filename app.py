from flask import Flask,request,jsonify,send_from_directory
import requests,re,math,sqlite3,os,json
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta

app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0 (compatible; OddsSignal/0.8)"}
JST=timezone(timedelta(hours=9))
DB=os.environ.get("DATABASE_PATH","/var/data/potun.db" if os.path.isdir("/var/data") else "/tmp/potun.db")

def con():
 c=sqlite3.connect(DB,timeout=30);c.row_factory=sqlite3.Row
 c.execute("PRAGMA busy_timeout=30000");c.execute("PRAGMA journal_mode=WAL");return c
def init():
 os.makedirs(os.path.dirname(DB),exist_ok=True);c=con()
 c.executescript("""
 CREATE TABLE IF NOT EXISTS races(race_key TEXT PRIMARY KEY,date TEXT,baba TEXT,baba_name TEXT,race INTEGER,start_iso TEXT,status TEXT DEFAULT 'reserved',created_at TEXT,result_checked INTEGER DEFAULT 0);
 CREATE TABLE IF NOT EXISTS snapshots(race_key TEXT,slot INTEGER,fetched_at TEXT,payload TEXT,PRIMARY KEY(race_key,slot));
 CREATE TABLE IF NOT EXISTS predictions(race_key TEXT,horse INTEGER,rank INTEGER,score REAL,pop INTEGER,odds REAL,d1 REAL,d2 REAL,agree REAL,created_at TEXT,PRIMARY KEY(race_key,horse));
 CREATE TABLE IF NOT EXISTS results(race_key TEXT PRIMARY KEY,first_horse INTEGER,second_horse INTEGER,third_horse INTEGER,fetched_at TEXT,payload TEXT);
 """);c.commit();c.close()
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
   m=re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)",z.replace(",",""))
   if m:
    v=float(m.group(1))
    if v>=1:
     o=v;break
  if o is not None:out[h]=o
 return [[h,o] for h,o in out.items()]
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
 c=con();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=?",(key,)).fetchall();c.close()
 S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
 if not all(x in S for x in ("15","10","5")):return []
 def mp(x):return {str(r["horse"]):r for r in x["rows"]}
 a,b,z=mp(S["15"]),mp(S["10"]),mp(S["5"]);out=[]
 for h,x in z.items():
  if h not in a or h not in b:continue
  d1=b[h]["base"]-a[h]["base"];d2=x["base"]-b[h]["base"];persist=sum(v["base"]>0 for v in (a[h],b[h],x))/3
  agree=sum(v>0 for v in (x["Q"],x["E"],x["T"]))/3;level=math.tanh(max(0,x["base"])/2);accel=math.tanh(max(0,d2)/1.5)
  score=round(100*(.40*level+.30*accel+.15*persist+.15*agree));out.append({**x,"d1":d1,"d2":d2,"agree":agree,"score":score})
 return sorted(out,key=lambda x:-x["score"])
def save_predictions(key):
 A=predictions(key);C=A[:3];c=con()
 c.execute("DELETE FROM predictions WHERE race_key=?",(key,))
 for i,x in enumerate(C,1):c.execute("INSERT OR REPLACE INTO predictions VALUES(?,?,?,?,?,?,?,?,?,?)",(key,x["horse"],i,x["score"],x["pop"],x["odds"],x["d1"],x["d2"],x["agree"],datetime.now(JST).isoformat()))
 c.commit();c.close();return C
def signal_strength(x):
 if x["score"]>=75 and x["agree"]>=2/3 and x["d2"]>0:return "STRONG"
 if x["score"]>=55 and x["agree"]>=1/3:return "MEDIUM"
 return "WEAK"

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
   c=con();ex=c.execute("SELECT 1 FROM snapshots WHERE race_key=? AND slot=?",(r["race_key"],slot)).fetchone();c.close()
   if not ex and slot-2<=mins<=slot+1:
    try:
     data=take(r);c=con();c.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?)",(r["race_key"],slot,data["fetched_at"],json.dumps(data,ensure_ascii=False)));c.commit();c.close();ev.append(f'{r["race_key"]}:{slot}')
     if slot==5:save_predictions(r["race_key"])
    except Exception as e:ev.append("ERR snapshot "+str(e))
  if mins < -3 and not r["result_checked"]:
   try:
    rs=parse_result(soup("RaceMarkTable",qfor(r)))
    if all(rs):
     c=con();c.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)",(r["race_key"],*rs,now.isoformat(),json.dumps(rs)));c.execute("UPDATE races SET result_checked=1,status='complete' WHERE race_key=?",(r["race_key"],));c.commit();c.close();ev.append(f'{r["race_key"]}:RESULT:{rs}')
   except Exception as e:ev.append("ERR result "+str(e))
 return ev

@app.route("/")
def home():return send_from_directory(".","index.html")
@app.route("/api/reserve",methods=["POST"])
def reserve():
 try:
  x=request.get_json(force=True);key=f'{x["date"]}:{x["baba"]}:{int(x["race"])}';st=datetime.fromisoformat(x["start_iso"]);c=con()
  c.execute("""INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(?,?,?,?,?,?,?,?,0)
  ON CONFLICT(race_key) DO UPDATE SET start_iso=excluded.start_iso,baba_name=excluded.baba_name,status='reserved',result_checked=0""",(key,x["date"],str(x["baba"]),x["baba_name"],int(x["race"]),st.isoformat(),"reserved",datetime.now(JST).isoformat()))
  c.commit();c.close();return jsonify(ok=True,race_key=key)
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/races")
def races():
 c=con();rr=[dict(x) for x in c.execute("SELECT * FROM races WHERE status!='cancelled' ORDER BY start_iso DESC LIMIT 50").fetchall()];out=[]
 for r in rr:
  ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=?",(r["race_key"],)).fetchall();S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
  stages={}
  if "15" in S:stages["15"]=point_signals(S["15"])
  if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
  if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
  pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=? ORDER BY rank",(r["race_key"],)).fetchall()]
  for p in pp:p["strength"]=signal_strength(p)
  rs=c.execute("SELECT * FROM results WHERE race_key=?",(r["race_key"],)).fetchone();result=dict(rs) if rs else None
  if result:
   places={result["first_horse"]:1,result["second_horse"]:2,result["third_horse"]:3}
   for p in pp:p["finish"]=places.get(p["horse"],0)
  out.append({**r,"slots":[int(x) for x in S],"stages":stages,"predictions":pp,"result":result})
 c.close();return jsonify(races=out)
@app.route("/api/cancel",methods=["POST"])
def cancel():
 try:
  key=request.get_json(force=True)["race_key"];c=con();c.execute("UPDATE races SET status='cancelled' WHERE race_key=?",(key,));c.commit();c.close();return jsonify(ok=True)
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/tick",methods=["GET","POST"])
def tick():
 try:return jsonify(ok=True,events=due(),at=datetime.now(JST).isoformat())
 except Exception as e:return jsonify(ok=False,error=str(e)),500
@app.route("/api/status")
def status():
 key=request.args["race_key"];c=con();r=c.execute("SELECT * FROM races WHERE race_key=?",(key,)).fetchone();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=?",(key,)).fetchall();S={str(x["slot"]):json.loads(x["payload"]) for x in ss}
 stages={}
 if "15" in S:stages["15"]=point_signals(S["15"])
 if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
 if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
 pp=[dict(x) for x in c.execute("SELECT * FROM predictions WHERE race_key=? ORDER BY rank",(key,)).fetchall()]
 for p in pp:p["strength"]=signal_strength(p)
 rs=c.execute("SELECT * FROM results WHERE race_key=?",(key,)).fetchone();result=dict(rs) if rs else None
 if result:
  places={result["first_horse"]:1,result["second_horse"]:2,result["third_horse"]:3}
  for p in pp:p["finish"]=places.get(p["horse"],0)
 c.close()
 return jsonify(race=dict(r) if r else None,snaps=S,stages=stages,predictions=pp,result=result)
@app.route("/api/repair-result",methods=["POST"])
def repair_result():
 try:
  key=request.get_json(force=True)["race_key"];c=con();r=c.execute("SELECT * FROM races WHERE race_key=?",(key,)).fetchone();c.close()
  if not r:return jsonify(ok=False,error="race not found"),404
  rs=parse_result(soup("RaceMarkTable",qfor(r)))
  if not all(rs):return jsonify(ok=False,error="official result not available"),409
  now=datetime.now(JST);c=con();c.execute("INSERT OR REPLACE INTO results VALUES(?,?,?,?,?,?)",(key,*rs,now.isoformat(),json.dumps(rs)));c.execute("UPDATE races SET result_checked=1,status='complete' WHERE race_key=?",(key,));c.commit();c.close()
  return jsonify(ok=True,result=rs)
 except Exception as e:return jsonify(ok=False,error=str(e)),500

@app.route("/api/learning")
def learning():
 c=con();n=c.execute("SELECT COUNT(*) n FROM results").fetchone()["n"];p=c.execute("SELECT p.*,r.first_horse,r.second_horse,r.third_horse FROM predictions p JOIN results r USING(race_key)").fetchall();c.close()
 total=len(p);hit=sum(x["horse"] in (x["first_horse"],x["second_horse"],x["third_horse"]) for x in p)
 return jsonify(completed_races=n,predictions=total,top3_hits=hit,top3_rate=(hit/total if total else None))
