from flask import Flask,request,jsonify,send_from_directory
import requests,re,math,os,json
import psycopg
from psycopg.rows import dict_row
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0 (compatible; OddsScopeNAR/13.0)"}
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
 for col,typ in (("parser_version","INTEGER DEFAULT 1"),("odds15","DOUBLE PRECISION"),("odds10","DOUBLE PRECISION"),("odds5","DOUBLE PRECISION"),("odds3","DOUBLE PRECISION"),
                 ("base3","DOUBLE PRECISION"),("d3","DOUBLE PRECISION"),("win_flow1","DOUBLE PRECISION"),("win_flow2","DOUBLE PRECISION"),("win_flow3","DOUBLE PRECISION"),("win_move","DOUBLE PRECISION"),
                 ("gap_score","DOUBLE PRECISION"),("isolation_score","DOUBLE PRECISION"),("float_score","DOUBLE PRECISION"),("cross_score","DOUBLE PRECISION"),("potun_score","DOUBLE PRECISION"),("accel_score","DOUBLE PRECISION"),
                 ("dna_score","DOUBLE PRECISION"),("edge_score","DOUBLE PRECISION"),("race_flow_score","DOUBLE PRECISION"),("scope_score","DOUBLE PRECISION")):
  try:
   c.execute(f"ALTER TABLE learning_samples ADD COLUMN {col} {typ}");c.commit()
  except Exception:c.rollback()
 c.close()
 c=con();done=c.execute("SELECT v FROM app_meta WHERE k='analysis_v11_1_late_accel'").fetchone()
 if not done:
  c.execute("UPDATE model_state SET status='COLLECTING',trained_races=0,trained_samples=0,weights=NULL,means=NULL,stds=NULL,heuristic_val=NULL,learned_val=NULL,updated_at=%s WHERE id=1",(datetime.now(JST).isoformat(),))
  c.execute("INSERT INTO app_meta(k,v) VALUES('analysis_v11_1_late_accel','1') ON CONFLICT(k) DO UPDATE SET v='1'")
 c.commit();c.close()
 # Ver.13 adds RACE FLOW as a learning feature. Reset only the fitted model; keep historical samples/results.
 c=con();done=c.execute("SELECT v FROM app_meta WHERE k='analysis_v13_race_flow'").fetchone()
 if not done:
  c.execute("UPDATE model_state SET status='COLLECTING',trained_races=0,trained_samples=0,weights=NULL,means=NULL,stds=NULL,heuristic_val=NULL,learned_val=NULL,updated_at=%s WHERE id=1",(datetime.now(JST).isoformat(),))
  c.execute("INSERT INTO app_meta(k,v) VALUES('analysis_v13_race_flow','1') ON CONFLICT(k) DO UPDATE SET v='1'")
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
 m=med(a);mad=med([abs(x-m) for x in a])
 if mad>=1e-9:return [(x-m)/(1.4826*mad) for x in a]
 # Sparse markets often have many equal values and one true outlier. MAD=0 must not erase it.
 mu=sum(a)/len(a);sd=math.sqrt(sum((x-mu)**2 for x in a)/len(a)) if a else 0
 return [0.0]*len(a) if sd<1e-9 else [(x-mu)/sd for x in a]

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
 # Longshots are allowed, but a very thin baseline needs stronger corroboration.
 p=max(0,int(pop)-9)*.035
 o=max(0.0,math.log(max(float(odds),1.0)/70.0))*.06
 return max(.76,min(1.0,1.0-p-o))

def _anom_scores(values):
 """Positive-outlier score: ordinary values stay near 0; only true race-relative anomalies rise."""
 if not values:return []
 vals=[float(v or 0) for v in values];m=med(vals);mad=med([abs(x-m) for x in vals])
 if mad<1e-9:
  mu=sum(vals)/len(vals);sd=math.sqrt(sum((x-mu)**2 for x in vals)/len(vals)) if vals else 0
  if sd<1e-9:return [0.0 for _ in vals]
  return [round(100.0*math.tanh(max(0.0,(v-mu)/sd)/2.0),1) for v in vals]
 out=[]
 for v in vals:
  z=max(0.0,(v-m)/(1.4826*mad))
  out.append(round(100.0*math.tanh(z/2.0),1))
 return out

def _market_structure(W):
 """Detect the user's visual odds cues: cliff (GAP) and isolated/POツン price position."""
 ordered=sorted(W,key=lambda x:(x[1],x[0]));n=len(ordered)
 gap_raw=[];iso_raw=[]
 for i,(h,o) in enumerate(ordered):
  lo=math.log(max(o,1.000001))
  after=math.log(ordered[i+1][1])-lo if i+1<n else 0.0
  before=lo-math.log(ordered[i-1][1]) if i>0 else 0.0
  gap_raw.append(max(0.0,after))
  # An isolated point must have space on BOTH sides; endpoints are not called isolated.
  iso_raw.append(max(0.0,min(before,after)) if 0<i<n-1 else 0.0)
 gaps=_anom_scores(gap_raw);isos=_anom_scores(iso_raw)
 return {h:{"gap_score":gaps[i],"isolation_score":isos[i],"gap_raw":gap_raw[i],"isolation_raw":iso_raw[i]} for i,(h,_) in enumerate(ordered)}

def _order3_prob(a,b,c,P):
 try:return P[a]*P[b]/max(1-P[a],1e-12)*P[c]/max(1-P[a]-P[b],1e-12)
 except:return 0.0

def _unordered3_prob(hs,P):
 import itertools
 return sum(_order3_prob(*perm,P) for perm in itertools.permutations(hs,3))

def _wide_prob(i,j,P):
 # Approximate probability that i and j both finish in the first 3 under Plackett-Luce.
 return sum(_unordered3_prob((i,j,k),P) for k in P if k not in (i,j))

def analyse(W,Q,E,T,Wide=None,Trio=None):
 inv={h:1/o for h,o in W};sm=sum(inv.values());P={h:v/sm for h,v in inv.items()};od=dict(W)
 pop={h:i+1 for i,(h,o) in enumerate(sorted(W,key=lambda x:(x[1],x[0])))}
 structure=_market_structure(W)
 markets={"Q":Q or [],"E":E or [],"W":Wide or [],"R":Trio or [],"T":T or []}
 B={h:{k:[] for k in markets} for h in P}
 def market(rows,k):
  mod=[];act=[];hh=[]
  for a in rows:
   hs,o=a[:-1],a[-1]
   try:
    if k=="Q":
     i,j=hs;pr=P[i]*P[j]/max(1-P[i],1e-12)+P[j]*P[i]/max(1-P[j],1e-12)
    elif k=="E":
     i,j=hs;pr=P[i]*P[j]/max(1-P[i],1e-12)
    elif k=="W":
     i,j=hs;pr=_wide_prob(i,j,P)
    elif k=="R":
     pr=_unordered3_prob(tuple(hs),P)
    else:
     i,j,z=hs;pr=_order3_prob(i,j,z,P)
   except:continue
   if pr>0 and o>0 and all(h in P for h in hs):mod.append(pr);act.append(1/o);hh.append(hs)
  if not hh:return
  ms=sum(mod);aa=sum(act)
  if ms<=0 or aa<=0:return
  modelp=[m/ms for m in mod];actualp=[a/aa for a in act]
  D=[math.log(max(ap,1e-15)/max(mp,1e-15)) for ap,mp in zip(actualp,modelp)]
  Z=band_rz(D,modelp)
  for z,hs in zip(Z,hh):
   for h in hs:
    if h in B:B[h][k].append(z)
 for k,rows in markets.items():market(rows,k)
 # Aggregate first, then compare win-pop rank with cross-market support rank (FLOAT).
 temp=[]
 for h,b in B.items():
  mk={k:agg(v) for k,v in b.items() if v}
  vals=list(mk.values());base=sum(vals)/len(vals) if vals else 0.0
  positive=sum(v>0 for v in vals);agree=positive/len(vals) if vals else 0.0
  cross_raw=(sum(max(0.0,v) for v in vals)/len(vals) if vals else 0.0)
  # POTUN: one combination being a much larger local outlier than the horse's normal combinations.
  spikes=[]
  for v in b.values():
   if len(v)>=3:
    vv=sorted(v,reverse=True);spikes.append(max(0.0,vv[0]-med(vv))*.7+max(0.0,vv[0]-vv[1])*.3)
  potun_raw=max(spikes) if spikes else 0.0
  temp.append((h,mk,base,agree,cross_raw,potun_raw))
 cross_rank={h:i+1 for i,(h,*_) in enumerate(sorted(temp,key=lambda z:(-z[2],pop[z[0]])))}
 potun_scores=_anom_scores([z[5] for z in temp])
 out=[]
 for idx,(h,mk,base,agree,cross_raw,potun_raw) in enumerate(temp):
  rank_adv=max(0.0,float(pop[h]-cross_rank[h])) if base>0 else 0.0
  float_score=round(min(100.0,70.0*math.tanh(rank_adv/max(2.5,len(temp)*.22))+30.0*math.tanh(max(0.0,base)/1.5)),1)
  cross_score=round(min(100.0,100.0*(.62*agree+.38*math.tanh(max(0.0,cross_raw)/1.8))),1)
  st=structure.get(h,{})
  out.append({"horse":h,"odds":od[h],"pop":pop[h],"win_share":P[h],
              "Q":mk.get("Q",0.0),"E":mk.get("E",0.0),"W":mk.get("W",0.0),"R":mk.get("R",0.0),"T":mk.get("T",0.0),
              "base":base,"cross_agree":agree,"cross_score":cross_score,"float_score":float_score,
              "potun_score":potun_scores[idx],"potun_raw":potun_raw,
              "gap_score":st.get("gap_score",0.0),"isolation_score":st.get("isolation_score",0.0),
              "reliability":tail_reliability(pop[h],od[h])})
 return out
DATA_VERSION=8



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

def _race_flow_fit(style,frame,front_count):
 # RACE FLOW is deliberately a supporting filter, not the market engine.
 base=_pace_fit(style,front_count)
 if base is None:return None
 adj=0.0
 if style in ("逃・先","先・好位"):
  if frame in (1,2,3):adj+=3.0
  elif frame in (7,8):adj-=2.0
 elif style=="差・追":
  if front_count>=4:adj+=4.0
  elif front_count<=2:adj-=3.0
 return max(20.0,min(85.0,base+adj))

def _race_flow_context(profiles):
 styles={"逃・先":0,"先・好位":0,"中団":0,"差・追":0,"不明":0}
 for p in profiles.values():styles[p.get("style") if p.get("style") in styles else "不明"]+=1
 front=styles["逃・先"]+styles["先・好位"]
 if front>=5:pace="HIGH";label="先行争い強め";bias="差し・中団が浮上しやすい"
 elif front==4:pace="HIGH";label="やや速め";bias="好位差しまで警戒"
 elif front==3:pace="BALANCED";label="平均想定";bias="極端な脚質バイアスは小さい"
 else:pace="SLOW";label="落ち着く想定";bias="前残りを警戒"
 return {"pace":pace,"label":label,"bias":bias,"front_count":front,"styles":styles}

def _flow_path(style,frame,front_count,horse):
 # Relative predicted position (1=front). This is a scenario visualization, not a claim of exact running order.
 base={"逃・先":1.5,"先・好位":3.5,"中団":6.5,"差・追":9.5}.get(style,7.0)
 fr=(int(frame)-4.5)*0.12 if frame else 0.0
 start=base+fr
 back=base + (0.2 if style=="逃・先" and front_count>=4 else -0.2 if style=="先・好位" else 0.0)
 turn=back + (-0.5 if style=="先・好位" and front_count>=4 else -0.8 if style=="中団" and front_count>=4 else 0.2 if style=="差・追" and front_count<=2 else 0.0)
 stretch=turn + (-1.5 if style=="差・追" and front_count>=4 else -0.9 if style=="中団" and front_count>=4 else -0.5 if style=="逃・先" and front_count<=2 else 0.5 if style=="逃・先" and front_count>=5 else 0.0)
 vals=[start,back,turn,stretch]
 return [round(max(1.0,min(12.0,v)),2) for v in vals]

def race_flow_summary(key,rankings=None):
 profiles=load_profiles(key);ctx=_race_flow_context(profiles);horses=[]
 feature_map={int(x.get("horse")):x for x in (rankings or [])}
 for h,p in sorted(profiles.items()):
  f=feature_map.get(int(h),{});style=p.get("style","不明");frame=p.get("frame")
  fit=f.get("race_flow_score")
  if fit is None:fit=_race_flow_fit(style,frame,ctx["front_count"])
  horses.append({"horse":int(h),"frame":frame,"name":p.get("name") or f"{h}番","style":style,"fit":None if fit is None else round(float(fit),1),
                 "path":_flow_path(style,frame,ctx["front_count"],h),"scope":f.get("adaptive_score") or f.get("scope_score")})
 # Convert the four phase positions to ranks so the animation stays readable regardless of field size.
 for phase in range(4):
  ordered=sorted(horses,key=lambda x:(x["path"][phase],x["horse"]))
  for rank,x in enumerate(ordered,1):x.setdefault("ranks",[None]*4)[phase]=rank
 return {**ctx,"phases":["START","BACK","3-4C","STRETCH"],"horses":horses,"note":"脚質・枠・先行馬数から作るシナリオ予測。実際の隊列を保証するものではなく、ODDS SCOPEの補助フィルターです。"}

def take(r):
 q=qfor(r)
 paths={"win":"OddsTanFuku","Q":"OddsUmLenFuku","E":"OddsUmLenTan","W":"OddsWide","R":"Odds3LenFuku","T":"Odds3LenTan"}
 pages={}
 # Parallel retrieval cuts the snapshot delay while keeping each market independently optional.
 with ThreadPoolExecutor(max_workers=4) as ex:
  fut={ex.submit(soup,path,q):k for k,path in paths.items()}
  for f in as_completed(fut):
   k=fut[f]
   try:pages[k]=f.result()
   except Exception:pages[k]=None
 if pages.get("win") is None:raise RuntimeError("単勝オッズ取得失敗")
 W=win(pages["win"]);Q=combo(pages["Q"],2) if pages.get("Q") else [];E=combo(pages["E"],2) if pages.get("E") else []
 Wide=combo(pages["W"],2) if pages.get("W") else [];Trio=combo(pages["R"],3) if pages.get("R") else [];T=combo(pages["T"],3) if pages.get("T") else []
 cnt={"win":len(W),"Q":len(Q),"E":len(E),"W":len(Wide),"R":len(Trio),"T":len(T)}
 if len(W)<3 or min(len(Q),len(E),len(T))==0:raise RuntimeError("主要オッズ取得不完全 "+str(cnt))
 market_sum=sum(1.0/o for _,o in W if o>0)
 if not 0.55<=market_sum<=2.20:raise RuntimeError(f"単勝オッズ検証NG market_sum={market_sum:.3f} counts={cnt}")
 return {"rows":analyse(W,Q,E,T,Wide,Trio),"counts":cnt,"win_market_sum":round(market_sum,4),"parser_version":DATA_VERSION,"fetched_at":datetime.now(JST).isoformat(timespec="seconds")}

def valid_payload(p):
 return isinstance(p,dict) and int(p.get("parser_version") or 0)>=DATA_VERSION

def _win_flow(prev_odds,cur_odds):
 if not prev_odds or not cur_odds or prev_odds<=0 or cur_odds<=0:return 0.0
 return math.log(prev_odds/cur_odds)  # positive = odds shortened / money entered

def point_signals(payload,prev=None):
 rows=payload.get("rows",[]);pm={str(x["horse"]):x for x in (prev or {}).get("rows",[])}
 raw=[];share_moves=[]
 for x in rows:
  p=pm.get(str(x["horse"])) if pm else None
  share_moves.append((float(x.get("win_share") or 0)-float(p.get("win_share") or 0)) if p else 0.0)
 zshares=rz(share_moves) if prev else [0.0]*len(rows)
 for x,sz,smv in zip(rows,zshares,share_moves):
  p=pm.get(str(x["horse"])) if pm else None
  delta=x["base"]-(p["base"] if p else x["base"])
  dna_now=.14*x.get("gap_score",0)+.16*x.get("isolation_score",0)+.14*x.get("float_score",0)+.20*x.get("cross_score",0)+.22*x.get("potun_score",0)
  move=0 if not p else max(0.0,100.0*math.tanh(max(0.0,sz)/2.0))
  score=max(0,min(100,round(.72*dna_now+.28*move)))
  pct=((p["odds"]/x["odds"])-1.0)*100 if p and x.get("odds") else None
  raw.append({**x,"delta":delta,"score":score,"prev_odds":p.get("odds") if p else None,"win_pct":pct,"share_delta":smv*100})
 return sorted(raw,key=lambda x:-x["score"])[:3]

def _model_blend_weight(state):
 # Learning never overrides the proven heuristic blindly. It is enabled only after
 # chronological holdout validation beats SCOPE, then ramps up gradually with data.
 if not state or state.get("status")!="ACTIVE" or not state.get("weights"):return 0.0
 try:
  h=float(state.get("heuristic_val") or 0);l=float(state.get("learned_val") or 0)
  if l<=h:return 0.0
  races=int(state.get("trained_races") or 0)
  maturity=max(0.0,min(1.0,(races-MIN_TRAIN_RACES)/220.0))
  gain=max(0.0,l-h)
  cap=float(result_guardrail().get("model_cap",.40));return round(min(cap,.25+.10*maturity+min(.05,gain*1.5)),3)
 except Exception:return 0.0

def _signal_profile(x,score=None):
 # Signal level asks a different question from rank: how many independent pieces of evidence agree?
 scope=float(score if score is not None else (x.get("adaptive_score") if x.get("adaptive_score") is not None else x.get("scope_score") or 0))
 dna=float(x.get("dna_score") or 0);flow=float(x.get("flow_score") or 0);accel=float(x.get("accel_score") or 0)
 pot=float(x.get("potun_score") or 0);cross=float(x.get("cross_score") or 0);rf=float(x.get("race_flow_score") or 50)
 edge=x.get("edge_score");edgev=float(edge) if edge is not None else 50.0
 conf=float(x.get("data_confidence") or 0);pop=int(x.get("pop") or 99)
 confirmations=sum((dna>=55,flow>=55,accel>=62,pot>=58,cross>=60,(edge is not None and edgev>=57),rf>=64))
 idx=.31*scope+.14*dna+.13*flow+.11*accel+.08*pot+.07*cross+.08*edgev+.08*rf
 idx+=max(0,confirmations-1)*1.8
 if pop>=7 and confirmations<2:idx-=5.0
 if pop>=10 and confirmations<3:idx-=3.0
 if pop<=3 and edge is not None and edgev>=58 and flow>=38:idx+=2.0
 if conf<50:idx-=4.0
 idx=max(0.0,min(100.0,idx))
 if idx>=72 and confirmations>=4:grade,label="S","PRIME"
 elif idx>=62 and confirmations>=3:grade,label="A","STRONG"
 elif idx>=52 and confirmations>=1:grade,label="B","SELECT"
 elif idx>=42:grade,label="C","WATCH"
 else:grade,label="D","LOW"
 return {"signal_index":round(idx,1),"signal_grade":grade,"signal_label":label,"confirmations":int(confirmations)}

_GUARD_CACHE={"at":None,"data":{"status":"COLLECTING","longshot_penalty":0.0,"model_cap":.40,"races":0}}

def result_guardrail(force=False):
 # Rolling result guard. It does not chase one race; it only activates after 30 completed races.
 global _GUARD_CACHE
 now=datetime.now(JST)
 if not force and _GUARD_CACHE.get("at") and (now-_GUARD_CACHE["at"]).total_seconds()<90:return _GUARD_CACHE["data"]
 data={"status":"COLLECTING","longshot_penalty":0.0,"model_cap":.40,"races":0,"scope_rate":None,"favorite_rate":None}
 try:
  c=con();keys=[x["race_key"] for x in c.execute("SELECT race_key FROM results ORDER BY fetched_at DESC LIMIT 30").fetchall()];keys=list(reversed(keys))
  if keys:
   ph=','.join(['%s']*len(keys))
   preds=[dict(x) for x in c.execute(f"SELECT p.*,r.first_horse,r.second_horse,r.third_horse FROM predictions p JOIN results r USING(race_key) WHERE p.race_key IN ({ph}) ORDER BY r.fetched_at,p.rank",keys).fetchall()]
   sam=[dict(x) for x in c.execute(f"SELECT * FROM learning_samples WHERE race_key IN ({ph}) AND COALESCE(parser_version,1)>=8",keys).fetchall()]
   bb={};ff={}
   for x in preds:bb.setdefault(x["race_key"],[]).append(x)
   for x in sam:ff.setdefault(x["race_key"],[]).append(x)
   dh=dp=fh=fp=0
   for k,rr in bb.items():
    z=sorted(rr,key=lambda q:int(q.get("rank") or 99))[:3]
    if not z:continue
    finish={int(z[0]["first_horse"]),int(z[0]["second_horse"]),int(z[0]["third_horse"])};dh+=sum(int(int(q["horse"]) in finish) for q in z);dp+=len(z)
   for k,rr in ff.items():
    z=sorted(rr,key=lambda q:int(q.get("pop") or 99))[:3];fh+=sum(int(q.get("label") or 0) for q in z);fp+=len(z)
   n=len(bb);sr=dh/dp if dp else None;fr=fh/fp if fp else None
   data.update({"races":n,"scope_rate":sr,"favorite_rate":fr})
   if n>=30 and sr is not None and fr is not None:
    if sr>=fr+.02:data.update(status="PASS",longshot_penalty=0.0,model_cap=.40)
    elif sr>=fr-.02:data.update(status="HOLD",longshot_penalty=2.0,model_cap=.30)
    else:data.update(status="REVIEW",longshot_penalty=5.0,model_cap=.20)
  c.close()
 except Exception:
  try:c.close()
  except Exception:pass
 _GUARD_CACHE={"at":now,"data":data};return data

def rank_features(A,model_state=None):
 state=model_state if model_state is not None else get_model_state();out=[]
 tmp=[]
 for src in A:
  x=dict(src);x["learned_prob"]=model_score(x,state);tmp.append(x)
 w=_model_blend_weight(state);valid=[float(x["learned_prob"]) for x in tmp if x.get("learned_prob") is not None]
 mu=sum(valid)/len(valid) if valid else 0.0
 sd=(sum((v-mu)**2 for v in valid)/len(valid))**.5 if valid else 0.0
 for x in tmp:
  base=max(0.0,min(1.0,float(x.get("scope_score") or 0)/100.0))
  lp=x.get("learned_prob")
  if w>0 and lp is not None and sd>1e-9:
   z=max(-3.0,min(3.0,(float(lp)-mu)/sd));model_signal=1.0/(1.0+math.exp(-1.15*z));final=(1.0-w)*base+w*model_signal
  else:model_signal=None;final=base
  x["model_signal"]=None if model_signal is None else round(model_signal*100,1);x["model_weight"]=round(w*100,1)
  provisional=round(final*100,1);prof=_signal_profile(x,provisional);pop=int(x.get("pop") or 99);edge=x.get("edge_score");flow=float(x.get("flow_score") or 0)
  adjust=0.0;guard=result_guardrail()
  if pop>=7 and prof["confirmations"]<2:adjust-=4.0+float(guard.get("longshot_penalty") or 0)
  elif pop>=7 and prof["confirmations"]==2:adjust-=1.5+.35*float(guard.get("longshot_penalty") or 0)
  if pop<=3 and edge is not None and float(edge)>=58 and flow>=40:adjust+=2.0
  final=max(0.0,min(1.0,(provisional+adjust)/100.0))
  x["adaptive_score"]=round(final*100,1);x.update(_signal_profile(x,x["adaptive_score"]))
  x["rank_score"]=final;x["score"]=x["adaptive_score"];x["strength"]=f'{x["signal_grade"]} {x["signal_label"]}'
  out.append(x)
 return sorted(out,key=lambda x:(-x["rank_score"],-float(x.get("signal_index") or 0),-float(x.get("scope_score") or 0),x.get("pop",99)))

def predictions(key,model_state=None):
 return rank_features(final_features(key),model_state)

def scope_label(x):
 prof=_signal_profile(x)
 return f'{prof["signal_grade"]} {prof["signal_label"]}'

def race_signal_summary(A):
 if not A:return {"grade":"D","label":"NO EDGE","action":"WAIT","note":"有効な候補データ待ち","qualified":0,"top_index":0,"spread":0}
 grade_order={"S":5,"A":4,"B":3,"C":2,"D":1};top=A[0];g=top.get("signal_grade") or "D";second=A[1] if len(A)>1 else None
 spread=float(top.get("adaptive_score") or 0)-float(second.get("adaptive_score") or 0) if second else 0.0
 qualified=sum(1 for x in A[:5] if grade_order.get(x.get("signal_grade","D"),1)>=3)
 meta={"S":("PRIME SIGNAL","PRIME","複数指標が高水準で一致"),"A":("STRONG SIGNAL","ACTIVE","複数の独立した裏付けあり"),"B":("SELECT SIGNAL","SELECTIVE","候補として注視する水準"),"C":("WATCH","WATCH","シグナルは限定的。順位は参考"),"D":("NO EDGE","PASS","明確なシグナルなし。無理に上位3頭を本命扱いしない")}
 label,action,note=meta.get(g,meta["D"])
 return {"grade":g,"label":label,"action":action,"note":note,"qualified":qualified,"top_index":round(float(top.get("signal_index") or 0),1),"spread":round(spread,1)}

def adaptive_candidates(A):
 return A[:3],race_signal_summary(A)

def save_predictions(key,slot=3):
 # Persist the latest top-3 for validation at each stage. The UI can show a wider candidate pool
 # at 10m/5m, while the predictions table keeps the current top-3 and is overwritten at 3m FINAL.
 A=stage_rankings(key,slot);C=A[:3]
 if not C:return []
 c=con();c.execute("DELETE FROM predictions WHERE race_key=%s",(key,))
 for i,x in enumerate(C,1):c.execute("INSERT INTO predictions VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(race_key,horse) DO UPDATE SET rank=EXCLUDED.rank,score=EXCLUDED.score,pop=EXCLUDED.pop,odds=EXCLUDED.odds,d1=EXCLUDED.d1,d2=EXCLUDED.d2,agree=EXCLUDED.agree,created_at=EXCLUDED.created_at",(key,x["horse"],i,x["score"],x["pop"],x["odds"],x.get("d1",0),x.get("d2",0),x.get("agree",0),datetime.now(JST).isoformat()))
 c.commit();c.close();return C

def signal_strength(x):return scope_label(x)

FEATURES=("base15","base10","base5","base3","d1","d2","d3","agree","persist","win_flow1","win_flow2","win_flow3","accel_score","gap_score","isolation_score","float_score","cross_score","potun_score","dna_score","edge_score","race_flow_score","log_odds","pop_scaled")
MIN_TRAIN_RACES=80
MIN_TRAIN_SAMPLES=600

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
 # 4分前は「直前加速」を見るための追加観測点。万一4分だけ欠けても3分FINALは止めない。
 if not all(k in S for k in ("15","10","5","3")):return []
 def mp(k):return {str(x["horse"]):x for x in S[k].get("rows",[])}
 a,b,c5,z=mp("15"),mp("10"),mp("5"),mp("3");c4=mp("4") if "4" in S else {};raw=[]
 profiles=load_profiles(key);front_count=sum(1 for p in profiles.values() if p.get("style") in ("逃・先","先・好位"))
 for h,x in z.items():
  if h not in a or h not in b or h not in c5:continue
  v4=c4.get(h)
  d1=b[h]["base"]-a[h]["base"];d2=c5[h]["base"]-b[h]["base"];d3=x["base"]-c5[h]["base"]
  seq=[a[h],b[h],c5[h]]+([v4] if v4 else [])+[x]
  persist=sum(v["base"]>0 for v in seq)/len(seq)
  market_keys=("Q","E","W","R","T")
  agree=sum(x.get(k,0)>0 for k in market_keys)/len(market_keys)
  o15,o10,o5,o3=map(float,(a[h]["odds"],b[h]["odds"],c5[h]["odds"],x["odds"]));o4=float(v4["odds"]) if v4 else None
  sh15,sh10,sh5,sh3=[float(v.get("win_share") or 0) for v in (a[h],b[h],c5[h],x)];sh4=float(v4.get("win_share") or 0) if v4 else None
  sf1=(sh10-sh15)*100;sf2=(sh5-sh10)*100
  if v4:
   sf3=(sh4-sh5)*100;sf4=(sh3-sh4)*100
   wf3a=_win_flow(o5,o4);wf3b=_win_flow(o4,o3)
   late_flow=.45*wf3a+.55*wf3b
   accel_raw=.08*sf1+.17*sf2+.30*sf3+.45*sf4+.25*max(0.0,sf3-sf2)+.45*max(0.0,sf4-sf3)
  else:
   sf3=(sh3-sh5)*100;sf4=None;wf3a=None;wf3b=None
   late_flow=_win_flow(o5,o3)
   accel_raw=.12*sf1+.28*sf2+.60*sf3+.40*max(0.0,sf3-sf2)
  wf1=_win_flow(o15,o10);wf2=_win_flow(o10,o5)
  p=profiles.get(int(h),{})
  raw.append({**x,"base15":a[h]["base"],"base10":b[h]["base"],"base5":c5[h]["base"],"base4":v4["base"] if v4 else None,"base3":x["base"],
   "d1":d1,"d2":d2,"d3":d3,"agree":agree,"persist":persist,"odds15":o15,"odds10":o10,"odds5":o5,"odds4":o4,"odds3":o3,
   "win_flow1":wf1,"win_flow2":wf2,"win_flow3":late_flow,"win_flow54":wf3a,"win_flow43":wf3b,"win_move":.15*wf1+.25*wf2+.60*late_flow,
   "share_flow1":sf1,"share_flow2":sf2,"share_flow3":sf3,"share_flow4":sf4,"accel_raw":accel_raw,"late_4m_ready":bool(v4),"profile":p})
 if not raw:return []
 az=rz([r["accel_raw"] for r in raw]);out=[]
 for r,zacc in zip(raw,az):
  accel_score=round(100.0*math.tanh(max(0.0,zacc)/2.0),1)
  current_level=100.0*math.tanh(max(0.0,r["base3"])/2.0)
  persistence=100.0*r["persist"]
  flow_score=round(max(0,min(100,.30*current_level+.32*accel_score+.23*float(r.get("cross_score") or 0)+.15*persistence)),1)
  gap=float(r.get("gap_score") or 0);iso=float(r.get("isolation_score") or 0);flo=float(r.get("float_score") or 0);cross=float(r.get("cross_score") or 0);pot=float(r.get("potun_score") or 0)
  dna=round(.12*gap+.16*iso+.14*flo+.18*cross+.22*pot+.18*accel_score,1)
  p=r.get("profile") or {};style=p.get("style","不明");pace_fit=_pace_fit(style,front_count);race_flow_fit=_race_flow_fit(style,p.get("frame"),front_count)
  form=p.get("form_score");dist=p.get("distance_score");course=p.get("course_score");jockey=p.get("jockey_score");cond=p.get("condition_score")
  edge,conf=_weighted_available([(form,.35),(dist,.20),(course,.15),(jockey,.10),(pace_fit,.10),(cond,.10)])
  out.append({**r,"flow_score":flow_score,"market_score":flow_score,"accel_score":accel_score,"dna_score":dna,
              "edge_score":round(edge,1) if edge is not None else None,"performance_score":round(edge,1) if edge is not None else None,
              "data_confidence":round(conf,1),"form_score":form,"distance_score":dist,"course_score":course,"jockey_score":jockey,
              "jockey_record":p.get("jockey_record",[0,0,0,0]),"jockey_has_data":bool(p.get("jockey_has_data",False)),
              "condition_score":cond,"pace_score":round(pace_fit,1) if pace_fit is not None else None,"race_flow_score":round(race_flow_fit,1) if race_flow_fit is not None else None,"style":style,"frame":p.get("frame"),
              "name":p.get("name") or f'{r["horse"]}番',"jockey":p.get("jockey",""),"recent_finishes":p.get("recent_finishes",[]),
              "body_weight":p.get("body_weight"),"body_diff":p.get("body_diff")})
 edge_rows=sorted([x for x in out if x.get("edge_score") is not None],key=lambda x:-x["edge_score"]);edge_rank={x["horse"]:i+1 for i,x in enumerate(edge_rows)}
 for x in out:
  er=edge_rank.get(x["horse"]);edge=x.get("edge_score");dna=x["dna_score"];flow=x["flow_score"]
  if er is None:value=max(0.0,min(100.0,45.0+max(0.0,dna-50)*.35))
  else:
   rank_gap=float(x.get("pop") or er)-float(er)
   value=max(0.0,min(100.0,50.0+rank_gap*5.0+max(0.0,dna-55.0)*.18+max(0.0,flow-55.0)*.10-max(0.0,48.0-(edge or 48.0))*.25))
  x["value_score"]=round(value,1)
  market_core=.74*dna+.26*flow
  x["market_core"]=round(market_core,1)
  if edge is not None:x["scope_score"]=round(.70*market_core+.25*edge+.05*value,1)
  else:x["scope_score"]=round(.88*market_core+.12*value,1)
  x["heuristic"]=x["scope_score"]
 return out



def staged_features(key,target_slot):
 # Live wagering features. Candidate display must not be blocked by missing earlier
 # snapshots or missing performance fields. Use every valid observation available up
 # to the current slot; missing ability/profile fields are excluded and weights renormalized.
 target_slot=int(target_slot)
 if target_slot not in (10,5,4,3):return []
 c=con();ss=c.execute("SELECT slot,payload FROM snapshots WHERE race_key=%s",(key,)).fetchall();c.close()
 S={str(x["slot"]):json.loads(x["payload"]) for x in ss};S={k:v for k,v in S.items() if valid_payload(v)}
 tk=str(target_slot)
 if tk not in S:return []
 ordered=(15,10,5,4,3);ti=ordered.index(target_slot)
 use_slots=[z for z in ordered[:ti+1] if str(z) in S]
 maps={z:{str(x["horse"]):x for x in S[str(z)].get("rows",[])} for z in use_slots}
 cur=maps[target_slot];profiles=load_profiles(key)
 front_count=sum(1 for p in profiles.values() if p.get("style") in ("逃・先","先・好位"))
 raw=[]
 for h,x in cur.items():
  obs=[(z,maps[z][h]) for z in use_slots if h in maps[z]]
  if not obs:continue
  by={z:r for z,r in obs};rows=[r for _,r in obs]
  persist=sum(float(v.get("base") or 0)>0 for v in rows)/len(rows)
  market_keys=("Q","E","W","R","T")
  agree=sum(float(x.get(k) or 0)>0 for k in market_keys)/len(market_keys)
  # Sequential support-share and odds movements using whatever snapshots actually exist.
  share_moves=[];odds_moves=[]
  for (_,p0),(_,p1) in zip(obs,obs[1:]):
   share_moves.append((float(p1.get("win_share") or 0)-float(p0.get("win_share") or 0))*100)
   odds_moves.append(_win_flow(float(p0.get("odds") or 0),float(p1.get("odds") or 0)))
  n=len(share_moves)
  weights={0:[],1:[1.0],2:[.35,.65],3:[.15,.30,.55],4:[.08,.17,.30,.45]}.get(n,[1.0/n]*n if n else [])
  accel_raw=sum(w*m for w,m in zip(weights,share_moves))
  if n>=2:accel_raw+=.40*max(0.0,share_moves[-1]-share_moves[-2])
  late_flow=odds_moves[-1] if odds_moves else 0.0
  wf1=_win_flow(float(by[15]["odds"]),float(by[10]["odds"])) if 15 in by and 10 in by else 0.0
  wf2=_win_flow(float(by[10]["odds"]),float(by[5]["odds"])) if 10 in by and 5 in by else 0.0
  d1=(float(by[10]["base"])-float(by[15]["base"])) if 15 in by and 10 in by else 0.0
  d2=(float(by[5]["base"])-float(by[10]["base"])) if 10 in by and 5 in by else 0.0
  d3=(float(by[3]["base"])-float(by[5]["base"])) if 5 in by and 3 in by else 0.0
  p=profiles.get(int(h),{})
  raw.append({**x,
   "base15":by.get(15,{}).get("base"),"base10":by.get(10,{}).get("base"),"base5":by.get(5,{}).get("base"),"base4":by.get(4,{}).get("base"),"base3":by.get(3,{}).get("base"),
   "d1":d1,"d2":d2,"d3":d3,"agree":agree,"persist":persist,
   "odds15":by.get(15,{}).get("odds"),"odds10":by.get(10,{}).get("odds"),"odds5":by.get(5,{}).get("odds"),"odds4":by.get(4,{}).get("odds"),"odds3":by.get(3,{}).get("odds"),
   "win_flow1":wf1,"win_flow2":wf2,"win_flow3":late_flow,"win_move":sum(odds_moves)/len(odds_moves) if odds_moves else 0.0,
   "share_flow1":share_moves[0] if len(share_moves)>0 else None,"share_flow2":share_moves[1] if len(share_moves)>1 else None,
   "share_flow3":share_moves[2] if len(share_moves)>2 else None,"share_flow4":share_moves[3] if len(share_moves)>3 else None,
   "accel_raw":accel_raw,"late_4m_ready":4 in by,"profile":p,"phase_slot":target_slot,"observed_slots":use_slots})
 if not raw:return []
 az=rz([r["accel_raw"] for r in raw]);out=[]
 for r,zacc in zip(raw,az):
  accel_score=round(100.0*math.tanh(max(0.0,zacc)/2.0),1)
  current_level=100.0*math.tanh(max(0.0,float(r.get("base") or 0))/2.0)
  persistence=100.0*r["persist"]
  flow_score=round(max(0,min(100,.30*current_level+.32*accel_score+.23*float(r.get("cross_score") or 0)+.15*persistence)),1)
  gap=float(r.get("gap_score") or 0);iso=float(r.get("isolation_score") or 0);flo=float(r.get("float_score") or 0);cross=float(r.get("cross_score") or 0);pot=float(r.get("potun_score") or 0)
  dna=round(.12*gap+.16*iso+.14*flo+.18*cross+.22*pot+.18*accel_score,1)
  p=r.get("profile") or {};style=p.get("style","不明");pace_fit=_pace_fit(style,front_count);race_flow_fit=_race_flow_fit(style,p.get("frame"),front_count)
  form=p.get("form_score");dist=p.get("distance_score");course=p.get("course_score");jockey=p.get("jockey_score");cond=p.get("condition_score")
  edge,conf=_weighted_available([(form,.35),(dist,.20),(course,.15),(jockey,.10),(pace_fit,.10),(cond,.10)])
  out.append({**r,"flow_score":flow_score,"market_score":flow_score,"accel_score":accel_score,"dna_score":dna,
              "edge_score":round(edge,1) if edge is not None else None,"performance_score":round(edge,1) if edge is not None else None,
              "data_confidence":round(conf,1),"form_score":form,"distance_score":dist,"course_score":course,"jockey_score":jockey,
              "jockey_record":p.get("jockey_record",[0,0,0,0]),"jockey_has_data":bool(p.get("jockey_has_data",False)),
              "condition_score":cond,"pace_score":round(pace_fit,1) if pace_fit is not None else None,"race_flow_score":round(race_flow_fit,1) if race_flow_fit is not None else None,"style":style,"frame":p.get("frame"),
              "name":p.get("name") or f'{r["horse"]}番',"jockey":p.get("jockey",""),"recent_finishes":p.get("recent_finishes",[]),
              "body_weight":p.get("body_weight"),"body_diff":p.get("body_diff")})
 edge_rows=sorted([x for x in out if x.get("edge_score") is not None],key=lambda x:-x["edge_score"]);edge_rank={x["horse"]:i+1 for i,x in enumerate(edge_rows)}
 for x in out:
  er=edge_rank.get(x["horse"]);edge=x.get("edge_score");dna=x["dna_score"];flow=x["flow_score"]
  if er is None:value=max(0.0,min(100.0,45.0+max(0.0,dna-50)*.35))
  else:
   rank_gap=float(x.get("pop") or er)-float(er)
   value=max(0.0,min(100.0,50.0+rank_gap*5.0+max(0.0,dna-55.0)*.18+max(0.0,flow-55.0)*.10-max(0.0,48.0-(edge or 48.0))*.25))
  x["value_score"]=round(value,1);market_core=.74*dna+.26*flow;x["market_core"]=round(market_core,1)
  if edge is not None:x["scope_score"]=round(.70*market_core+.25*edge+.05*value,1)
  else:x["scope_score"]=round(.88*market_core+.12*value,1)
  x["heuristic"]=x["scope_score"]
 return out

def stage_rankings(key,slot):
 slot=int(slot)
 if slot==3:
  complete=final_features(key)
  if complete:return rank_features(complete,get_model_state())
 return rank_features(staged_features(key,slot),{})

def store_learning_samples(key,rs):
 rows=final_features(key)
 if not rows:return 0
 top=set(rs);now=datetime.now(JST).isoformat();c=con()
 for x in rows:
  c.execute("""INSERT INTO learning_samples
   (race_key,horse,label,base15,base10,base5,base3,d1,d2,d3,agree,persist,odds15,odds10,odds5,odds3,win_flow1,win_flow2,win_flow3,win_move,odds,pop,heuristic,created_at,parser_version,
    gap_score,isolation_score,float_score,cross_score,potun_score,accel_score,dna_score,edge_score,race_flow_score,scope_score)
   VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
   ON CONFLICT(race_key,horse) DO UPDATE SET label=EXCLUDED.label,base15=EXCLUDED.base15,base10=EXCLUDED.base10,base5=EXCLUDED.base5,base3=EXCLUDED.base3,d1=EXCLUDED.d1,d2=EXCLUDED.d2,d3=EXCLUDED.d3,agree=EXCLUDED.agree,persist=EXCLUDED.persist,
    odds15=EXCLUDED.odds15,odds10=EXCLUDED.odds10,odds5=EXCLUDED.odds5,odds3=EXCLUDED.odds3,win_flow1=EXCLUDED.win_flow1,win_flow2=EXCLUDED.win_flow2,win_flow3=EXCLUDED.win_flow3,win_move=EXCLUDED.win_move,
    odds=EXCLUDED.odds,pop=EXCLUDED.pop,heuristic=EXCLUDED.heuristic,created_at=EXCLUDED.created_at,parser_version=EXCLUDED.parser_version,gap_score=EXCLUDED.gap_score,isolation_score=EXCLUDED.isolation_score,float_score=EXCLUDED.float_score,
    cross_score=EXCLUDED.cross_score,potun_score=EXCLUDED.potun_score,accel_score=EXCLUDED.accel_score,dna_score=EXCLUDED.dna_score,edge_score=EXCLUDED.edge_score,race_flow_score=EXCLUDED.race_flow_score,scope_score=EXCLUDED.scope_score""",
   (key,x["horse"],1 if x["horse"] in top else 0,x["base15"],x["base10"],x["base5"],x["base3"],x["d1"],x["d2"],x["d3"],x["agree"],x["persist"],
    x["odds15"],x["odds10"],x["odds5"],x["odds3"],x["win_flow1"],x["win_flow2"],x["win_flow3"],x["win_move"],x["odds"],x["pop"],x["heuristic"],now,DATA_VERSION,
    x["gap_score"],x["isolation_score"],x["float_score"],x["cross_score"],x["potun_score"],x["accel_score"],x["dna_score"],x.get("edge_score"),x.get("race_flow_score"),x["scope_score"]))
 c.commit();c.close();return len(rows)

def _vec(r):
 edge=float(r.get("edge_score") if r.get("edge_score") is not None else 50.0)
 return [float(r["base15"]),float(r["base10"]),float(r["base5"]),float(r.get("base3") or 0),float(r["d1"]),float(r["d2"]),float(r.get("d3") or 0),
         float(r["agree"]),float(r["persist"]),float(r.get("win_flow1") or 0),float(r.get("win_flow2") or 0),float(r.get("win_flow3") or 0),
         float(r.get("accel_score") or 0),float(r.get("gap_score") or 0),float(r.get("isolation_score") or 0),float(r.get("float_score") or 0),float(r.get("cross_score") or 0),
         float(r.get("potun_score") or 0),float(r.get("dna_score") or 0),edge,float(r.get("race_flow_score") if r.get("race_flow_score") is not None else 50.0),math.log(max(float(r["odds"]),1.0001)),min(float(r["pop"]),18.0)/18.0]

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

def _rank_metrics(rows,score_fn):
 by={}
 for r in rows:by.setdefault(r["race_key"],[]).append(r)
 hits=total=race_hits=top1_hits=perfect=0
 for rr in by.values():
  pick=sorted(rr,key=score_fn,reverse=True)[:3]
  if not pick:continue
  h=sum(int(x["label"]) for x in pick);hits+=h;total+=len(pick);race_hits+=int(h>0);top1_hits+=int(pick[0]["label"]);perfect+=int(h>=3)
 n=len(by)
 return {"capture":hits/total if total else 0.0,"race_hit":race_hits/n if n else 0.0,"top1_place":top1_hits/n if n else 0.0,"perfect3":perfect/n if n else 0.0,"races":n}

def maybe_train():
 c=con()
 rows=[dict(x) for x in c.execute("SELECT * FROM learning_samples WHERE COALESCE(parser_version,1)>=8 ORDER BY created_at,race_key,horse").fetchall()]
 races=[x["race_key"] for x in c.execute("SELECT race_key FROM results ORDER BY fetched_at").fetchall()
        if c.execute("SELECT 1 FROM learning_samples WHERE race_key=%s AND COALESCE(parser_version,1)>=8 LIMIT 1",(x["race_key"],)).fetchone()]
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
 hm=_rank_metrics(val,lambda r:float(r["heuristic"]))
 lm=_rank_metrics(val,lambda r:_prob(r,w,mn,sd))
 h=hm["capture"];l=lm["capture"];oldv=int(state.get("version") or 0)
 # Result-first adoption: require a meaningful capture improvement and do not allow
 # top-pick or race-hit quality to deteriorate materially on chronological holdout.
 adopt=(l>=h+.015 and lm["top1_place"]>=hm["top1_place"]-.02 and lm["race_hit"]>=hm["race_hit"]-.01)
 if adopt:
  version=oldv+1
  c=con();c.execute("""UPDATE model_state SET version=%s,status='ACTIVE',trained_races=%s,trained_samples=%s,
   weights=%s,means=%s,stds=%s,heuristic_val=%s,learned_val=%s,updated_at=%s WHERE id=1""",
   (version,len(uniq),len(rows),json.dumps(w),json.dumps(mn),json.dumps(sd),h,l,datetime.now(JST).isoformat()))
  c.commit();c.close();return {"status":"ACTIVE","version":version,"heuristic_val":h,"learned_val":l,"heuristic_metrics":hm,"learned_metrics":lm}
 c=con();c.execute("""UPDATE model_state SET status=%s,trained_races=%s,trained_samples=%s,heuristic_val=%s,learned_val=%s,updated_at=%s WHERE id=1""",
  ("KEEP_HEURISTIC",len(uniq),len(rows),h,l,datetime.now(JST).isoformat()));c.commit();c.close()
 return {"status":"KEEP_HEURISTIC","version":oldv,"heuristic_val":h,"learned_val":l,"heuristic_metrics":hm,"learned_metrics":lm}

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
  r={k:x.get(k) for k in ("base15","base10","base5","base3","d1","d2","d3","agree","persist","win_flow1","win_flow2","win_flow3","accel_score","gap_score","isolation_score","float_score","cross_score","potun_score","dna_score","edge_score","race_flow_score","odds","pop")}
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
  for slot in (15,10,5,4,3):
   c=con();oldrow=c.execute("SELECT payload FROM snapshots WHERE race_key=%s AND slot=%s",(r["race_key"],slot)).fetchone();c.close()
   ex=False
   if oldrow:
    try:ex=valid_payload(json.loads(oldrow["payload"]))
    except:ex=False
   if slot==10: in_window=(8.90 < mins <= 11.20)
   elif slot==5: in_window=(4.35 < mins <= 6.20)
   elif slot==4: in_window=(3.35 < mins <= 4.60)
   elif slot==3: in_window=(1.85 <= mins <= 3.60)
   else: in_window=(slot-1.25 <= mins <= slot+0.72)
   if not ex and in_window:
    try:
     data=take(r);c=con();c.execute("""INSERT INTO snapshots VALUES(%s,%s,%s,%s)
      ON CONFLICT(race_key,slot) DO UPDATE SET fetched_at=EXCLUDED.fetched_at,payload=EXCLUDED.payload""",
      (r["race_key"],slot,data["fetched_at"],json.dumps(data,ensure_ascii=False)));c.commit();c.close();ev.append(f'{r["race_key"]}:{slot}')
     if slot in (10,5,4,3):save_predictions(r["race_key"],slot)
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
  mins_left=(st-datetime.now(JST)).total_seconds()/60.0
  if mins_left<3.0:
   return jsonify(ok=False,error="発走3分前を切っているため新規予約できません。3分以上前に予約してください"),409
  # Late reservations are allowed. Start from the earliest snapshot window that is still realistically reachable.
  if mins_left>=13.75:start_stage=15
  elif mins_left>8.90:start_stage=10
  elif mins_left>4.35:start_stage=5
  elif mins_left>3.35:start_stage=4
  else:start_stage=3
  c=con()
  c.execute("""INSERT INTO races(race_key,date,baba,baba_name,race,start_iso,status,created_at,result_checked) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,0)
  ON CONFLICT(race_key) DO UPDATE SET start_iso=excluded.start_iso,baba_name=excluded.baba_name,status='reserved',result_checked=0""",(key,x["date"],str(x["baba"]),x["baba_name"],int(x["race"]),st.isoformat(),"reserved",datetime.now(JST).isoformat()))
  c.commit();c.close()
  # Return immediately. The browser triggers /api/tick once after reservation, while cron remains the durable fallback.
  return jsonify(ok=True,race_key=key,profile_status="queued",start_stage=start_stage,minutes_left=round(mins_left,1))
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
    RAW={};S={}
    for x in ss:
     try:
      p=json.loads(x["payload"]);RAW[str(x["slot"])]=p
      if valid_payload(p):S[str(x["slot"])]=p
     except Exception:
      pass
    stages={}
    if "15" in S:stages["15"]=point_signals(S["15"])
    if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
    if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
    if "4" in S:stages["4"]=point_signals(S["4"],S.get("5"))
    if "3" in S:stages["3"]=point_signals(S["3"],S.get("4") or S.get("5"))
    rs=c.execute("SELECT * FROM results WHERE race_key=%s",(r["race_key"],)).fetchone();result=dict(rs) if rs else None
    out.append({**r,"slots":[int(x) for x in RAW],"current_slots":[int(x) for x in S],"legacy":bool(set(RAW)-set(S)),"stages":stages,"result":result})
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
  RAW={};S={}
  for x in ss:
   try:
    p=json.loads(x["payload"]);RAW[str(x["slot"])]=p
    if valid_payload(p):S[str(x["slot"])]=p
   except Exception:
    pass
  stages={}
  if "15" in S:stages["15"]=point_signals(S["15"])
  if "10" in S:stages["10"]=point_signals(S["10"],S.get("15"))
  if "5" in S:stages["5"]=point_signals(S["5"],S.get("10"))
  if "4" in S:stages["4"]=point_signals(S["4"],S.get("5"))
  if "3" in S:stages["3"]=point_signals(S["3"],S.get("4") or S.get("5"))
  full=all(k in S for k in ("15","10","5","3"))
  if "3" in S:phase_slot=3;phase="FINAL";phase_note="3分前。直前変化と学習シグナルを反映して最終候補3頭を確定します。"
  elif "4" in S:phase_slot=4;phase="LATE UPDATE";phase_note="4分前。SHORTLISTから急変・ポツン・断層を反映して候補3頭まで絞ります。"
  elif "5" in S:phase_slot=5;phase="SHORTLIST";phase_note="5分前。候補を4頭程度に絞る段階です。ここではまだ最終確定しません。"
  elif "10" in S:phase_slot=10;phase="CANDIDATE";phase_note="10分前。まず候補5頭を表示します。ここから5→4→3分で絞り込みます。"
  else:phase_slot=None;phase="WAIT";phase_note="10分前CANDIDATE待ちです。"
  model_state=get_model_state()
  if phase_slot==3 and full:
   features=final_features(key);rank_state=model_state
  elif phase_slot:
   features=staged_features(key,phase_slot);rank_state={}
  else:
   features=[];rank_state={}
  rankings=rank_features(features,rank_state) if features else []
  fm={x["horse"]:x for x in rankings}
  for p in pp:
   if p["horse"] in fm:p.update({k:fm[p["horse"]].get(k) for k in ("odds15","odds10","odds5","odds4","odds3","win_flow1","win_flow2","win_flow3","win_move","market_score","flow_score","performance_score","edge_score","value_score","scope_score","data_confidence","gap_score","isolation_score","float_score","cross_score","potun_score","accel_score","dna_score","market_core","form_score","distance_score","course_score","jockey_score","jockey_record","jockey_has_data","condition_score","pace_score","race_flow_score","style","frame","name","jockey","recent_finishes","body_weight","body_diff","adaptive_score","learned_prob","model_signal","model_weight","phase_slot","signal_index","signal_grade","signal_label","confirmations")})
   p["strength"]=signal_strength(p)
  result=dict(rs) if rs else None
  result_eval=None
  if result:
   places={result["first_horse"]:1,result["second_horse"]:2,result["third_horse"]:3}
   for p in pp:p["finish"]=places.get(p["horse"],0)
   delivered=sorted(pp,key=lambda z:int(z.get("rank") or 99))[:3]
   hits=[int(z["horse"]) for z in delivered if int(z["horse"]) in places]
   top1_place=bool(delivered and int(delivered[0]["horse"]) in places)
   hit_count=len(hits)
   if hit_count==3:result_grade,result_label="A","3/3 CAPTURE"
   elif hit_count==2:result_grade,result_label="B","2/3 CAPTURE"
   elif hit_count==1:result_grade,result_label="C","1/3 CAPTURE"
   else:result_grade,result_label="D","0/3"
   def _baseline_hits(seq,key,reverse=True):
    z=sorted(seq,key=lambda q:float(q.get(key) if q.get(key) is not None else (-1e9 if reverse else 1e9)),reverse=reverse)[:3]
    return sum(int(int(q["horse"]) in places) for q in z),[int(q["horse"]) for q in z]
   fav_hits,fav_picks=_baseline_hits(rankings,"pop",False) if rankings else (None,[])
   dna_hits,dna_picks=_baseline_hits(rankings,"dna_score",True) if rankings else (None,[])
   edge_valid=[q for q in rankings if q.get("edge_score") is not None]
   edge_hits,edge_picks=_baseline_hits(edge_valid,"edge_score",True) if edge_valid else (None,[])
   result_eval={"picks":[int(z["horse"]) for z in delivered],"hits":hits,"hit_count":hit_count,"top1_place":top1_place,"perfect3":hit_count==3,"grade":result_grade,"label":result_label,
                "favorite":{"hits":fav_hits,"picks":fav_picks},"dna":{"hits":dna_hits,"picks":dna_picks},"edge":{"hits":edge_hits,"picks":edge_picks}}
  profile_ready=any(x.get("name") and x.get("frame") for x in features) if features else bool(load_profiles(key))
  display_predictions=[]
  if rankings:
   # Staged narrowing: show a wider pool early, then narrow as late money arrives.
   # 10m=5 candidates, 5m=4 shortlist, 4m=3 late candidates, 3m=3 final.
   limit={10:5,5:4,4:3,3:3}.get(phase_slot,3)
   selected=list(rankings[:max(1,limit-1)])
   # During CANDIDATE/SHORTLIST keep one 4-9 popularity horse if the market-DNA supports it,
   # so a useful mid-price signal is not hidden merely by raw SCOPE rank.
   if phase_slot in (10,5):
    mid=next((x for x in rankings if 4<=int(x.get("pop") or 99)<=9 and x not in selected),None)
    if mid:selected.append(mid)
   for x in rankings:
    if len(selected)>=limit:break
    if x not in selected:selected.append(x)
   for i,x in enumerate(selected[:limit],1):
    y=dict(x);y["rank"]=i;y["strength"]=scope_label(y);display_predictions.append(y)
  # Historical compatibility: old parser payloads stay visible, but are never fed into Ver.11 DNA/learning.
  profiles=load_profiles(key)
  latest_key=next((k for k in ("3","4","5","10","15") if k in RAW),None)
  latest=(RAW.get(latest_key) or {}) if latest_key else {}
  latest_rows={int(x.get("horse")):x for x in latest.get("rows",[]) if x.get("horse") is not None}
  legacy_predictions=[]
  for oldp in pp:
   h=int(oldp.get("horse") or 0);pr=profiles.get(h,{})
   row=latest_rows.get(h,{})
   legacy_predictions.append({"horse":h,"rank":oldp.get("rank"),"legacy_score":oldp.get("score"),"pop":oldp.get("pop") or row.get("pop"),
      "odds":oldp.get("odds") or row.get("odds"),"frame":pr.get("frame"),"name":pr.get("name") or f"{h}番",
      "jockey":pr.get("jockey","") ,"style":pr.get("style","不明")})
  legacy_market=[]
  for h,row in latest_rows.items():
   pr=profiles.get(h,{})
   legacy_market.append({"horse":h,"pop":row.get("pop"),"odds":row.get("odds"),"base":row.get("base"),
      "frame":pr.get("frame"),"name":pr.get("name") or f"{h}番","jockey":pr.get("jockey","")})
  legacy_market.sort(key=lambda x:((x.get("pop") if x.get("pop") is not None else 99),x["horse"]))
  legacy_active=bool(RAW) and not bool(S)
  legacy={"active":legacy_active,"slots":sorted([int(k) for k in RAW]),"latest_slot":int(latest_key) if latest_key else None,
          "predictions":legacy_predictions,"market_rows":legacy_market,
          "note":"旧版保存データを表示中。Ver.11 ODDS DNAの再計算・学習には使用しません。"}
  race_signal=race_signal_summary(rankings)
  race_flow=race_flow_summary(key,rankings)
  guardrail=result_guardrail()
  return jsonify(race=dict(r) if r else None,snaps=S,display_snaps=RAW,stages=stages,predictions=display_predictions,rankings=rankings,result=result,result_eval=result_eval,race_signal=race_signal,race_flow=race_flow,guardrail=guardrail,profile_ready=profile_ready,legacy=legacy,signal_phase=phase,phase_slot=phase_slot,phase_note=phase_note,model={"status":model_state.get("status","COLLECTING"),"version":model_state.get("version",0),"trained_races":model_state.get("trained_races",0),"trained_samples":model_state.get("trained_samples",0),"heuristic_val":model_state.get("heuristic_val"),"learned_val":model_state.get("learned_val"),"blend_weight":(_model_blend_weight(model_state) if phase_slot==3 and full else 0.0)})
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
 samples=c.execute("SELECT COUNT(*) n FROM learning_samples WHERE COALESCE(parser_version,1)>=8").fetchone()["n"]
 lraces=c.execute("SELECT COUNT(DISTINCT race_key) n FROM learning_samples WHERE COALESCE(parser_version,1)>=8").fetchone()["n"]
 rows=[dict(x) for x in c.execute("SELECT * FROM learning_samples WHERE COALESCE(parser_version,1)>=8 ORDER BY race_key,horse").fetchall()]
 s=dict(c.execute("SELECT * FROM model_state WHERE id=1").fetchone());c.close()
 by={}
 for x in rows:by.setdefault(x["race_key"],[]).append(x)
 def metric(key,reverse=True):
  hits=picks=race_hits=0
  for rr in by.values():
   valid=[x for x in rr if x.get(key) is not None]
   if not valid:continue
   chosen=sorted(valid,key=lambda x:float(x[key]),reverse=reverse)[:3]
   h=sum(int(x["label"]) for x in chosen);hits+=h;picks+=len(chosen);race_hits+=int(h>0)
  n=len(by)
  return {"pick_rate":hits/picks if picks else None,"race_hit_rate":race_hits/n if n else None,"hits":hits,"picks":picks}
 scope_m=metric("scope_score",True);dna_m=metric("dna_score",True);fav_m=metric("pop",False);edge_m=metric("edge_score",True)
 # Delivered FINAL picks: evaluate what the user actually saw, not a hindsight recomputation.
 c=con();pr=[dict(x) for x in c.execute("""SELECT p.*,r.first_horse,r.second_horse,r.third_horse,r.fetched_at AS result_at FROM predictions p JOIN results r USING(race_key)
              WHERE EXISTS(SELECT 1 FROM learning_samples l WHERE l.race_key=p.race_key AND COALESCE(l.parser_version,1)>=8) ORDER BY r.fetched_at,p.rank""").fetchall()];c.close()
 def delivered_metrics(items):
  bb={}
  for x in items:bb.setdefault(x["race_key"],[]).append(x)
  hits=picks=race_hits=top1_hits=perfect=0
  for rr in bb.values():
   rr=sorted(rr,key=lambda z:int(z.get("rank") or 99))[:3]
   if not rr:continue
   finish={int(rr[0]["first_horse"]),int(rr[0]["second_horse"]),int(rr[0]["third_horse"])}
   h=sum(int(int(z["horse"]) in finish) for z in rr);hits+=h;picks+=len(rr);race_hits+=int(h>0);top1_hits+=int(int(rr[0]["horse"]) in finish);perfect+=int(h==3)
  n=len(bb)
  return {"races":n,"pick_rate":hits/picks if picks else None,"race_hit_rate":race_hits/n if n else None,"top1_place_rate":top1_hits/n if n else None,"perfect3_rate":perfect/n if n else None,"hits":hits,"picks":picks}
 delivered_all=delivered_metrics(pr)
 keys_order=[]
 for x in pr:
  if x["race_key"] not in keys_order:keys_order.append(x["race_key"])
 recent_keys=set(keys_order[-30:]);recent_pr=[x for x in pr if x["race_key"] in recent_keys];recent_delivered=delivered_metrics(recent_pr)
 recent_rows=[x for x in rows if x["race_key"] in recent_keys]
 def recent_baseline(key,reverse=True):
  bb={}
  for x in recent_rows:bb.setdefault(x["race_key"],[]).append(x)
  hits=picks=race_hits=top1_hits=0
  for rr in bb.values():
   valid=[x for x in rr if x.get(key) is not None]
   if not valid:continue
   chosen=sorted(valid,key=lambda x:float(x[key]),reverse=reverse)[:3];h=sum(int(x["label"]) for x in chosen)
   hits+=h;picks+=len(chosen);race_hits+=int(h>0);top1_hits+=int(chosen and chosen[0]["label"])
  n=len(bb)
  return {"races":n,"pick_rate":hits/picks if picks else None,"race_hit_rate":race_hits/n if n else None,"top1_place_rate":top1_hits/n if n else None}
 recent_fav=recent_baseline("pop",False)
 if recent_delivered["races"]<30:system_status="COLLECTING"
 else:
  dc=recent_delivered.get("pick_rate") or 0;fc=recent_fav.get("pick_rate") or 0;dr=recent_delivered.get("race_hit_rate") or 0;fr=recent_fav.get("race_hit_rate") or 0
  if dc>=fc+.02 and dr>=fr-.02:system_status="PASS"
  elif dc>=fc-.02:system_status="HOLD"
  else:system_status="REVIEW"
 total=len(pr);hit=delivered_all["hits"];signal_races=delivered_all["races"];race_hit=(delivered_all["race_hit_rate"] or 0)*signal_races
 return jsonify(completed_races=lraces,predictions=total,top3_hits=hit,top3_rate=delivered_all["pick_rate"],signal_races=signal_races,race_hit_rate=delivered_all["race_hit_rate"],
  top1_place_rate=delivered_all["top1_place_rate"],perfect3_rate=delivered_all["perfect3_rate"],recent_performance=recent_delivered,recent_favorite=recent_fav,system_status=system_status,
  learning_races=lraces,learning_samples=samples,model_status=s["status"],model_version=s["version"],min_train_races=MIN_TRAIN_RACES,min_train_samples=MIN_TRAIN_SAMPLES,
  heuristic_val=s["heuristic_val"],learned_val=s["learned_val"],updated_at=s["updated_at"],model_blend_weight=_model_blend_weight(s),scope_baseline=scope_m,dna_baseline=dna_m,favorite_baseline=fav_m,edge_baseline=edge_m,guardrail=result_guardrail(force=True))
