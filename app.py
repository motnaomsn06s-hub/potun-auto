from flask import Flask,request,jsonify,send_from_directory
import requests,re,math
from bs4 import BeautifulSoup
from datetime import datetime,timezone,timedelta
app=Flask(__name__,static_folder='.')
BASE="https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/"
UA={"User-Agent":"Mozilla/5.0"}
JST=timezone(timedelta(hours=9))
def get(path,q):
 r=requests.get(BASE+path,params=q,headers=UA,timeout=20);r.raise_for_status();r.encoding=r.apparent_encoding or r.encoding;return BeautifulSoup(r.text,"html.parser")
def win(s):
 out={}
 for tr in s.find_all("tr"):
  c=[" ".join(x.stripped_strings) for x in tr.find_all(["td","th"])]
  nums=[]
  for x in c:
   try: nums.append(float(x.replace(",","")))
   except: pass
  if len(nums)>=2:
   h=int(nums[0])
   if 1<=h<=18:
    for o in nums[1:]:
     if o>=1:out[h]=o;break
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
 b=sorted(a);n=len(b)
 return 0 if not n else b[n//2] if n%2 else (b[n//2-1]+b[n//2])/2
def rz(a):
 m=med(a);mad=med([abs(x-m) for x in a])
 return [0]*len(a) if mad<1e-9 else [(x-m)/(1.4826*mad) for x in a]
def agg(a):
 a=sorted([x for x in a if x>0],reverse=True)
 return .5*(a[0] if len(a)>0 else 0)+.3*(a[1] if len(a)>1 else 0)+.2*(a[2] if len(a)>2 else 0)
def analyse(W,Q,E,T):
 inv={h:1/o for h,o in W};sm=sum(inv.values());P={h:v/sm for h,v in inv.items()};od=dict(W)
 pop={h:i+1 for i,(h,o) in enumerate(sorted(W,key=lambda x:(x[1],x[0])))};B={h:{k:[] for k in "QET"} for h in P}
 def market(rows,k):
  mod=[];act=[];hsx=[]
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
   if pr>0 and o>0:mod.append(pr);act.append(1/o);hsx.append(hs)
  if not hsx:return
  ms=sum(mod);aa=sum(act);ds=[math.log(max(a/aa,1e-15)/max(m/ms,1e-15)) for a,m in zip(act,mod)]
  for z,hs in zip(rz(ds),hsx):
   for h in hs:
    if h in B:B[h][k].append(z)
 market(Q,"Q");market(E,"E");market(T,"T")
 out=[]
 for h,b in B.items():
  q,e,t=agg(b["Q"]),agg(b["E"]),agg(b["T"])
  out.append({"horse":h,"odds":od[h],"pop":pop[h],"Q":q,"E":e,"T":t,"base":(q+e+t)/3})
 return out
@app.route("/")
def home():return send_from_directory(".","index.html")
@app.route("/api/snapshot")
def snap():
 q={"k_babaCode":request.args["baba"],"k_raceDate":request.args["date"].replace("-","/"),"k_raceNo":request.args["race"]}
 try:
  W=win(get("OddsTanFuku",q));Q=combo(get("OddsUmLenFuku",q),2);E=combo(get("OddsUmLenTan",q),2);T=combo(get("Odds3LenTan",q),3)
  counts={"win":len(W),"Q":len(Q),"E":len(E),"T":len(T)}
  if len(W)<3 or min(len(Q),len(E),len(T))==0:return jsonify(error="オッズ取得不完全。保存しません",counts=counts),422
  return jsonify(rows=analyse(W,Q,E,T),counts=counts,fetched_at=datetime.now(JST).isoformat(timespec="seconds"))
 except Exception as e:return jsonify(error=str(e)),500
