from flask import Flask, request, jsonify, send_from_directory
import requests, re, math
from bs4 import BeautifulSoup
app=Flask(__name__, static_folder='.')
BASE='https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/'
UA={'User-Agent':'Mozilla/5.0'}

def fetch(path,params):
 r=requests.get(BASE+path,params=params,headers=UA,timeout=20); r.raise_for_status(); r.encoding=r.apparent_encoding or r.encoding; return r.text

def parse_win(html):
 s=BeautifulSoup(html,'html.parser'); out=[]
 for tr in s.find_all('tr'):
  cells=[' '.join(x.stripped_strings) for x in tr.find_all(['td','th'])]
  if len(cells)>=4:
   nums=re.findall(r'^\d+$',cells[1] if len(cells)>1 else '')
   if nums:
    try:
     h=int(cells[1]); o=float(cells[3].replace(',',''))
     if o>0: out.append([h,o])
    except: pass
 return list({x[0]:x for x in out}.values())

def parse_combo(html,n):
 text=BeautifulSoup(html,'html.parser').get_text('\n',strip=True)
 pat=(r'(?<!\d)(\d{1,2})-(\d{1,2})' + (r'-(\d{1,2})' if n==3 else '') + r'\s+([\d,.]+)\s+(\d+)')
 out=[]
 for m in re.finditer(pat,text):
  g=m.groups(); hs=list(map(int,g[:n])); o=float(g[n].replace(',',''))
  if o>0: out.append(hs+[o])
 return out

def med(a):
 a=sorted(a); n=len(a)
 return 0 if not n else a[n//2] if n%2 else (a[n//2-1]+a[n//2])/2

def rz(a):
 if not a:return []
 m=med(a); mad=med([abs(x-m) for x in a]) or 1e-6
 return [(x-m)/(1.4826*mad) for x in a]

def agg(a):
 a=sorted([x for x in a if x>0],reverse=True)
 return .5*(a[0] if len(a)>0 else 0)+.3*(a[1] if len(a)>1 else 0)+.2*(a[2] if len(a)>2 else 0)

def analyze(W,Q,E,T):
 raw={h:1/o for h,o in W}; sm=sum(raw.values()); p={h:v/sm for h,v in raw.items()}; B={h:{'T':[],'E':[],'Q':[]} for h in p}
 def market(rows,k):
  ds=[]; meta=[]
  for a in rows:
   hs=a[:-1]; o=a[-1]; pr=0
   try:
    if k=='Q': i,j=hs; pr=2*p[i]*p[j]
    elif k=='E': i,j=hs; pr=p[i]*p[j]/max(1-p[i],1e-9)
    else: i,j,z=hs; pr=p[i]*p[j]/max(1-p[i],1e-9)*p[z]/max(1-p[i]-p[j],1e-9)
   except KeyError: continue
   if pr>0 and o>0: ds.append(math.log((1/pr)/o)); meta.append(hs)
  for z,hs in zip(rz(ds),meta):
   for h in hs:
    if h in B:B[h][k].append(z)
 for rows,k in [(Q,'Q'),(E,'E'),(T,'T')]: market(rows,k)
 A=[]
 for h,b in B.items():
  tt,ee,qq=agg(b['T']),agg(b['E']),agg(b['Q']); r=.55*tt+.25*ee+.20*qq; A.append({'horse':h,'T':tt,'E':ee,'Q':qq,'raw':r})
 lo=min(x['raw'] for x in A); hi=max(x['raw'] for x in A); d=hi-lo or 1
 for x in A:x['score']=round(100*(x['raw']-lo)/d)
 return sorted(A,key=lambda x:x['score'],reverse=True)

@app.route('/')
def home(): return send_from_directory('.', 'index.html')
@app.route('/api/analyze')
def api():
 date=request.args.get('date'); baba=request.args.get('baba'); race=request.args.get('race')
 params={'k_babaCode':baba,'k_raceDate':date.replace('-','/'),'k_raceNo':race}
 try:
  W=parse_win(fetch('OddsTanFuku',params))
  Q=parse_combo(fetch('OddsUmLenFuku',params),2)
  E=parse_combo(fetch('OddsUmLenTan',params),2)
  T=parse_combo(fetch('Odds3LenTan',params),3)
  if len(W)<3: return jsonify(error='単勝オッズを取得できませんでした',counts={'win':len(W),'quinella':len(Q),'exacta':len(E),'trifecta':len(T)}),422
  return jsonify(rows=analyze(W,Q,E,T),counts={'win':len(W),'quinella':len(Q),'exacta':len(E),'trifecta':len(T)})
 except Exception as e:return jsonify(error=str(e)),500
if __name__=='__main__': app.run(host='0.0.0.0',port=8787,debug=False)
