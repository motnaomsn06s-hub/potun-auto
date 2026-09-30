from flask import Flask,request,jsonify,send_from_directory
import requests,re,math,threading,time,sqlite3,os
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta

app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0"}
JST=timezone(timedelta(hours=9))
DB="/tmp/potun_v06.db"
LOCK=threading.Lock()

def db():
 c=sqlite3.connect(DB,timeout=30)
 c.row_factory=sqlite3.Row
 c.execute("PRAGMA busy_timeout=30000")
 c.execute("PRAGMA journal_mode=WAL")
 return c

def init_db():
 with LOCK:
  c=db()
  c.execute("""CREATE TABLE IF NOT EXISTS races(
  id INTEGER PRIMARY KEY AUTOINCREMENT, race_key TEXT UNIQUE,date TEXT,baba TEXT,baba_name TEXT,race INTEGER,
  start_iso TEXT,status TEXT DEFAULT 'reserved',created_at TEXT)""")
  c.execute("""CREATE TABLE IF NOT EXISTS snaps(
  id INTEGER PRIMARY KEY AUTOINCREMENT,race_key TEXT,slot INTEGER,fetched_at TEXT,payload TEXT,
  UNIQUE(race_key,slot))""")
  c.commit();c.close()

init_db()
def fetch(path,q):
 r=requests.get(BASE+path,params=q,headers=UA,timeout=20);r.raise_for_status()
 r.encoding=r.apparent_encoding or r.encoding
 return BeautifulSoup(r.text,"html.parser")

def parse_win(s):
 out={}
 for tr in s.find_all("tr"):
  c=[" ".join(x.stripped_strings) for x in tr.find_all(["td","th"])]
  # locate horse number then first plausible odds later in row
  for i,x in enumerate(c):
   if re.fullmatch(r"\d{1,2}",x or ""):
    h=int(x)
    if not 1<=h<=18: continue
    vals=[]
    for z in c[i+1:]:
     try: vals.append(float(z.replace(",","")))
     except: pass
    odds=next((v for v in vals if v>=1.0),None)
    if odds is not None: out[h]=odds
    break
 return [[h,o] for h,o in out.items()]

def parse_combo(s,n):
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
 b=sorted(a);n=len(b)
 return 0 if not n else b[n//2] if n%2 else (b[n//2-1]+b[n//2])/2
def robust_z(a):
 if not a:return []
 m=med(a);mad=med([abs(x-m) for x in a])
 return [0]*len(a) if mad<1e-9 else [(x-m)/(1.4826*mad) for x in a]
def agg(a):
 a=sorted([x for x in a if x>0],reverse=True)
 return .5*(a[0] if len(a)>0 else 0)+.3*(a[1] if len(a)>1 else 0)+.2*(a[2] if len(a)>2 else 0)

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
  D=[math.log(max(a/aa,1e-15)/max(m/ms,1e-15)) for a,m in zip(act,mod)]
  for z,hs in zip(robust_z(D),hh):
   for h in hs:
    if h in B:B[h][k].append(z)
 market(Q,"Q");market(E,"E");market(T,"T")
 out=[]
 for h,b in B.items():
  q,e,t=agg(b["Q"]),agg(b["E"]),agg(b["T"])
  out.append({"horse":h,"odds":od[h],"pop":pop[h],"Q":q,"E":e,"T":t,"base":(q+e+t)/3})
 return out

def snapshot(r):
 q={"k_babaCode":r["baba"],"k_raceDate":r["date"].replace("-","/"),"k_raceNo":r["race"]}
 W=parse_win(fetch("OddsTanFuku",q));Q=parse_combo(fetch("OddsUmLenFuku",q),2)
 E=parse_combo(fetch("OddsUmLenTan",q),2);T=parse_combo(fetch("Odds3LenTan",q),3)
 counts={"win":len(W),"Q":len(Q),"E":len(E),"T":len(T)}
 if len(W)<3 or min(len(Q),len(E),len(T))==0:raise RuntimeError("オッズ取得不完全")
 return {"rows":analyse(W,Q,E,T),"counts":counts,"fetched_at":datetime.now(JST).isoformat(timespec="seconds")}

def worker():
 while True:
  try:
   now=datetime.now(JST)
   c=db()
   races=[dict(x) for x in c.execute("SELECT * FROM races WHERE status!='done'").fetchall()]
   c.close()
   for r in races:
    start=datetime.fromisoformat(r["start_iso"])
    mins=(start-now).total_seconds()/60
    for slot in (15,10,5):
     c=db()
     exists=c.execute("SELECT 1 FROM snaps WHERE race_key=? AND slot=?",(r["race_key"],slot)).fetchone()
     c.close()
     if not exists and slot-1.0 <= mins <= slot+0.25:
      try:
       import json
       data=snapshot(r)
       with LOCK:
        c=db()
        c.execute("INSERT OR IGNORE INTO snaps(race_key,slot,fetched_at,payload) VALUES(?,?,?,?)",
                  (r["race_key"],slot,data["fetched_at"],json.dumps(data,ensure_ascii=False)))
        c.commit();c.close()
      except Exception:
       pass
    if mins < 3:
     with LOCK:
      c=db();c.execute("UPDATE races SET status='done' WHERE race_key=?",(r["race_key"],));c.commit();c.close()
  except Exception:
   pass
  time.sleep(15)




def run_due_checks():
 now=datetime.now(JST)
 c=db()
 races=[dict(x) for x in c.execute("SELECT * FROM races WHERE status!='done'").fetchall()]
 c.close()
 events=[]
 for r in races:
  start=datetime.fromisoformat(r["start_iso"])
  mins=(start-now).total_seconds()/60
  for slot in (15,10,5):
   c=db();exists=c.execute("SELECT 1 FROM snaps WHERE race_key=? AND slot=?",(r["race_key"],slot)).fetchone();c.close()
   # Allow late catch-up within 90 sec so a request near target still records.
   if not exists and slot-1.5 <= mins <= slot+0.5:
    try:
     import json
     data=snapshot(r)
     with LOCK:
      c=db();c.execute("INSERT OR IGNORE INTO snaps(race_key,slot,fetched_at,payload) VALUES(?,?,?,?)",
       (r["race_key"],slot,data["fetched_at"],json.dumps(data,ensure_ascii=False)));c.commit();c.close()
     events.append(f'{r["race_key"]}:{slot}')
    except Exception as e:
     events.append(f'{r["race_key"]}:{slot}:ERR:{e}')
  if mins < 3:
   with LOCK:
    c=db();c.execute("UPDATE races SET status='done' WHERE race_key=?",(r["race_key"],));c.commit();c.close()
 return events

@app.route("/api/tick",methods=["GET","POST"])
def api_tick():
 try:
  return jsonify(ok=True,events=run_due_checks(),at=datetime.now(JST).isoformat(timespec="seconds"))
 except Exception as e:
  return jsonify(ok=False,error=f"{type(e).__name__}: {e}"),500

@app.route("/")
def home():return send_from_directory(".","index.html")

@app.route("/api/reserve",methods=["POST"])
def reserve():
 try:
  x=request.get_json(force=True) or {}
  for k in ("date","baba","baba_name","race","start_iso"):
   if not x.get(k): return jsonify(error=f"missing {k}"),400
  start=datetime.fromisoformat(str(x["start_iso"]))
  if start.tzinfo is None: start=start.replace(tzinfo=JST)
  key=f'{x["date"]}:{x["baba"]}:{int(x["race"])}'
  with LOCK:
   c=db()
   c.execute("""INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at)
   VALUES(?,?,?,?,?,?,?,?)
   ON CONFLICT(race_key) DO UPDATE SET
    start_iso=excluded.start_iso,baba_name=excluded.baba_name,status='reserved'""",
    (key,x["date"],str(x["baba"]),x["baba_name"],int(x["race"]),start.isoformat(),"reserved",datetime.now(JST).isoformat()))
   c.commit();c.close()
  return jsonify(ok=True,race_key=key,start_iso=start.isoformat())
 except Exception as e:
  return jsonify(error=f"予約保存エラー: {type(e).__name__}: {e}"),500

@app.route("/api/status")
def status():
 import json
 key=request.args["race_key"];c=db()
 r=c.execute("SELECT * FROM races WHERE race_key=?",(key,)).fetchone()
 ss=c.execute("SELECT * FROM snaps WHERE race_key=? ORDER BY slot DESC",(key,)).fetchall();c.close()
 if not r:return jsonify(error="not found"),404
 snaps={str(s["slot"]):json.loads(s["payload"]) for s in ss}
 return jsonify(race=dict(r),snaps=snaps)

@app.route("/api/list")
def list_races():
 c=db();rr=[dict(x) for x in c.execute("SELECT * FROM races ORDER BY start_iso DESC LIMIT 20").fetchall()];c.close()
 return jsonify(races=rr)
