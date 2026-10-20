
import http.server, urllib.request, json, datetime
UP="https://potun-auto.onrender.com"
def get(path):
    with urllib.request.urlopen(urllib.request.Request(UP+path,headers={"User-Agent":"ui-check"}),timeout=60) as r:return r.status,r.headers.get('Content-Type','application/json'),r.read()
def races():
    return json.loads(get('/api/races?limit=200')[2])['races']
def today():return (datetime.datetime.utcnow()+datetime.timedelta(hours=9)).date().isoformat()
class H(http.server.SimpleHTTPRequestHandler):
  def send(self,obj):
    b=json.dumps(obj,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b)
  def do_GET(self):
    p=self.path.split('?')[0]
    try:
      if p=='/api/day':
        d=today();out=[]
        for r in races():
          if r['date']!=d:continue
          res=r.get('result');out.append({**r,'status':'complete' if r.get('result_saved') else r['status'],'slots':r.get('current_slots',[]),'result':{'order':[res['first_horse'],res['second_horse'],res['third_horse']],'places':[1,2,3]} if res else None,'picks':[]})
        return self.send({'ok':True,'date':d,'races':out})
      if p=='/api/days':
        g={}
        for r in races():
          x=g.setdefault(r['date'],{'date':r['date'],'races':0,'complete':0,'venues':set()});x['races']+=1;x['complete']+=bool(r.get('result_saved'));x['venues'].add(r['baba_name'])
        return self.send({'ok':True,'days':[{**v,'venues':'・'.join(sorted(v['venues']))} for k,v in sorted(g.items(),reverse=True)]})
      if p.startswith('/api/') or p.startswith('/race3d') or p.startswith('/assets') or p.startswith('/apple-touch'):
        s,ct,b=get(self.path);self.send_response(s);self.send_header('Content-Type',ct);self.end_headers();self.wfile.write(b);return
    except Exception as e:
      self.send_response(502);self.end_headers();self.wfile.write(str(e).encode());return
    return super().do_GET()
  def log_message(self,*a):pass
http.server.ThreadingHTTPServer(('127.0.0.1',8765),H).serve_forever()
