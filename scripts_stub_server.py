
import json, http.server, os
ROI={"ok":True,"races":67,"results_with_payouts":67,"late_money_races":40,"late_money_horses":480,
 "strategies":[{"label":l,"key":k,"scope":{"races":67,"roi":r1,"hit_rate":h1,"ci95":[r1-0.2,r1+0.25]},"favorite":{"races":67,"roi":r2,"hit_rate":h2,"ci95":[r2-0.1,r2+0.1]}} for l,k,r1,h1,r2,h2 in
 [("単勝","win",0.812,0.31,0.776,0.52),("複勝","place",1.034,0.74,0.842,0.9),("ワイドBOX","wide",0.95,0.4,0.81,0.55),("馬連BOX","quinella",0.72,0.18,0.79,0.3),("3連複","trio",1.21,0.09,0.74,0.21)]],
 "late_money":[{"label":"急に売れた","horses":52,"place_rate":0.42,"place_roi":1.12},{"label":"売れた","horses":140,"place_rate":0.31,"place_roi":0.88},{"label":"横ばい","horses":210,"place_rate":0.22,"place_roi":0.81},{"label":"売れなかった","horses":78,"place_rate":0.09,"place_roi":0.55}]}
LEARN={"recent_performance":{"pick_rate":0.47,"races":30,"top1_place_rate":0.6,"race_hit_rate":0.83,"perfect3_rate":0.07},"recent_favorite":{"pick_rate":0.52,"race_hit_rate":0.9},"system_status":"HOLD","model_status":"COLLECTING","learning_races":67,"min_train_races":80,"learning_samples":828,"guardrail":{"status":"HOLD"}}
RACES={"races":[{"race_key":"2026-10-20:18:7","date":"2026-10-20","baba_name":"浦和","race":7,"start_iso":"2026-10-20T15:27:00+09:00","status":"reserved","current_slots":[15,10,5],"source":"auto"},{"race_key":"2026-10-20:36:9","date":"2026-10-20","baba_name":"門別","race":9,"start_iso":"2026-10-20T19:05:00+09:00","status":"reserved","current_slots":[],"source":"auto"}],"has_more":False}
class H(http.server.SimpleHTTPRequestHandler):
  def do_GET(self):
    p=self.path.split('?')[0]
    if p.startswith('/api/'):
      body={"/api/roi":ROI,"/api/learning":LEARN,"/api/races":RACES}.get(p,{"ok":True})
      b=json.dumps(body).encode();self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(b);return
    return super().do_GET()
  def do_POST(self):self.do_GET()
http.server.ThreadingHTTPServer(('127.0.0.1',8765),H).serve_forever()
