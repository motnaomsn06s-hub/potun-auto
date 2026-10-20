
import http.server, urllib.request
UP="https://potun-auto.onrender.com"
class H(http.server.SimpleHTTPRequestHandler):
  def do_GET(self):
    p=self.path.split('?')[0]
    if p.startswith('/api/') or p.startswith('/race3d') or p.startswith('/assets') or p.startswith('/apple-touch'):
      try:
        with urllib.request.urlopen(urllib.request.Request(UP+self.path,headers={"User-Agent":"ui-check"}),timeout=90) as r:
          b=r.read();self.send_response(r.status);self.send_header('Content-Type',r.headers.get('Content-Type','application/json'));self.end_headers();self.wfile.write(b);return
      except Exception as e:
        self.send_response(502);self.end_headers();self.wfile.write(str(e).encode());return
    return super().do_GET()
  def log_message(self,*a):pass
http.server.ThreadingHTTPServer(('127.0.0.1',8765),H).serve_forever()
