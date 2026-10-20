
import http.server, urllib.request, json, datetime
UP="https://potun-auto.onrender.com"
JST=datetime.timezone(datetime.timedelta(hours=9))
def get(path):
    with urllib.request.urlopen(urllib.request.Request(UP+path,headers={"User-Agent":"ui-check"}),timeout=90) as r:return r.status,r.headers.get('Content-Type','application/json'),r.read()
class H(http.server.SimpleHTTPRequestHandler):
  def do_GET(self):
    p=self.path.split('?')[0]
    try:
      if p=='/api/day':
        d=json.loads(get('/api/day?date=2026-10-20')[2]);now=datetime.datetime.now(JST)
        pivot=datetime.datetime(2026,10,20,18,40,tzinfo=JST);off=now-pivot
        for r in d['races']:
          t=datetime.datetime.fromisoformat(r['start_iso']);m=(t-pivot).total_seconds()/60
          r['start_iso']=(t+off).isoformat()
          if m>-1 and r['status']!='cancelled':
            r['status']='reserved';r['result']=None;r['picks']=[] if m>3 else r['picks']
            win={15:(13.75,15.72),10:(8.9,11.2),5:(4.6,6.2),4:(3.6,4.6),3:(1.85,3.6)}
            r['slots']=[s for s,(lo,hi) in win.items() if m<lo]
        d['date']=now.date().isoformat()
        for r in d['races']:r['date']=d['date']
        b=json.dumps(d,ensure_ascii=False).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b);return
      if p.startswith('/api/') or p.startswith('/race3d') or p.startswith('/assets') or p.startswith('/apple-touch'):
        s,ct,b=get(self.path);self.send_response(s);self.send_header('Content-Type',ct);self.end_headers();self.wfile.write(b);return
    except Exception as e:
      self.send_response(502);self.end_headers();self.wfile.write(str(e).encode());return
    return super().do_GET()
  def log_message(self,*a):pass
http.server.ThreadingHTTPServer(('127.0.0.1',8765),H).serve_forever()
