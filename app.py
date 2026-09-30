from flask import Flask, request, jsonify, send_from_directory
import requests, re, math
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta

app = Flask(__name__, static_folder='.')
BASE = 'https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/'
UA = {'User-Agent': 'Mozilla/5.0'}

def fetch(path, params):
    r = requests.get(BASE + path, params=params, headers=UA, timeout=20)
    r.raise_for_status()
    r.encoding = r.apparent_encoding or r.encoding
    return r.text

def parse_win(html):
    s = BeautifulSoup(html, 'html.parser')
    out = []
    for tr in s.find_all('tr'):
        cells = [' '.join(x.stripped_strings) for x in tr.find_all(['td','th'])]
        if len(cells) >= 4 and re.fullmatch(r'\d+', cells[1] if len(cells)>1 else ''):
            try:
                h = int(cells[1])
                o = float(cells[3].replace(',',''))
                if o > 0:
                    out.append([h,o])
            except Exception:
                pass
    return list({x[0]:x for x in out}.values())

def parse_combo(html, n):
    text = BeautifulSoup(html, 'html.parser').get_text('\n', strip=True)
    pat = (r'(?<!\d)(\d{1,2})-(\d{1,2})' +
           (r'-(\d{1,2})' if n == 3 else '') +
           r'\s+([\d,.]+)\s+(\d+)')
    out = []
    for m in re.finditer(pat, text):
        g = m.groups()
        hs = list(map(int, g[:n]))
        try:
            o = float(g[n].replace(',',''))
            if o > 0:
                out.append(hs + [o])
        except Exception:
            pass
    return out

def median(a):
    a = sorted(a)
    n = len(a)
    if not n: return 0.0
    return a[n//2] if n % 2 else (a[n//2-1] + a[n//2]) / 2

def robust_z(vals):
    if not vals: return []
    m = median(vals)
    mad = median([abs(x-m) for x in vals])
    if mad < 1e-12:
        return [0.0 for _ in vals]
    return [(x-m)/(1.4826*mad) for x in vals]

def weighted_top3_positive(vals):
    a = sorted([x for x in vals if x > 0], reverse=True)
    return .5*(a[0] if len(a)>0 else 0) + .3*(a[1] if len(a)>1 else 0) + .2*(a[2] if len(a)>2 else 0)

def normal_cdf_score(z):
    # 50 = race-average level, 84 ≈ +1SD, 98 ≈ +2SD.
    z = max(-4.0, min(4.0, z))
    return round(100 * 0.5 * (1 + math.erf(z / math.sqrt(2))))

def analyze(W, Q, E, T):
    # Win market -> baseline strengths
    raw = {h: 1/o for h,o in W}
    sm = sum(raw.values())
    p = {h:v/sm for h,v in raw.items()}
    win_odds = {h:o for h,o in W}
    pop_order = sorted(W, key=lambda x:(x[1], x[0]))
    popularity = {h:i+1 for i,(h,_) in enumerate(pop_order)}

    B = {h:{'T':[], 'E':[], 'Q':[]} for h in p}

    def market(rows, k):
        model_probs, actual_inv, metas = [], [], []
        for a in rows:
            hs, o = a[:-1], a[-1]
            try:
                if k == 'Q':
                    i,j = hs
                    # unordered pair under sequential win-strength baseline
                    pr = p[i]*p[j]/max(1-p[i],1e-12) + p[j]*p[i]/max(1-p[j],1e-12)
                elif k == 'E':
                    i,j = hs
                    pr = p[i]*p[j]/max(1-p[i],1e-12)
                else:
                    i,j,z = hs
                    pr = p[i] * p[j]/max(1-p[i],1e-12) * p[z]/max(1-p[i]-p[j],1e-12)
            except (KeyError, ValueError):
                continue
            if pr > 0 and o > 0:
                model_probs.append(pr)
                actual_inv.append(1/o)
                metas.append(hs)

        if not metas:
            return

        # Normalize within each market: removes market-wide takeout/scale.
        ms = sum(model_probs)
        aas = sum(actual_inv)
        pm = [x/ms for x in model_probs]
        pa = [x/aas for x in actual_inv]
        # Positive D = more support than win-strength baseline predicts.
        d = [math.log(max(a,1e-15)/max(m,1e-15)) for a,m in zip(pa,pm)]
        zvals = robust_z(d)
        for z,hs in zip(zvals, metas):
            for h in hs:
                if h in B:
                    B[h][k].append(z)

    for rows,k in [(Q,'Q'), (E,'E'), (T,'T')]:
        market(rows,k)

    rows = []
    for h,b in B.items():
        tt = weighted_top3_positive(b['T'])
        ee = weighted_top3_positive(b['E'])
        qq = weighted_top3_positive(b['Q'])
        # v0.4 LOCK: equal market weights. No result-based tuning.
        components = [tt, ee, qq]
        composite = sum(components) / 3.0
        positive_markets = sum(x > 0 for x in components)
        rows.append({
            'horse':h, 'win_odds':win_odds[h], 'popularity':popularity[h],
            'T':tt, 'E':ee, 'Q':qq, 'z':composite,
            'score':normal_cdf_score(composite),
            'positive_markets':positive_markets
        })

    rows.sort(key=lambda x:(-x['score'], -x['positive_markets'], x['popularity']))
    # Candidate rule is fixed before results:
    # score >= 70 AND positive signal in at least 2 of 3 markets.
    candidates = [x for x in rows if x['score'] >= 70 and x['positive_markets'] >= 2][:3]
    return rows, candidates

@app.route('/')
def home():
    return send_from_directory('.', 'index.html')

@app.route('/api/analyze')
def api():
    date = request.args.get('date','')
    baba = request.args.get('baba','')
    race = request.args.get('race','')
    params = {'k_babaCode':baba, 'k_raceDate':date.replace('-','/'), 'k_raceNo':race}
    try:
        W = parse_win(fetch('OddsTanFuku', params))
        Q = parse_combo(fetch('OddsUmLenFuku', params), 2)
        E = parse_combo(fetch('OddsUmLenTan', params), 2)
        T = parse_combo(fetch('Odds3LenTan', params), 3)
        counts = {'win':len(W), 'quinella':len(Q), 'exacta':len(E), 'trifecta':len(T)}
        if len(W) < 3:
            return jsonify(error='単勝オッズを取得できませんでした', counts=counts), 422
        if min(len(Q),len(E),len(T)) == 0:
            return jsonify(error='組合せオッズの取得が不完全です。解析を中止しました。', counts=counts), 422
        rows, candidates = analyze(W,Q,E,T)
        jst = timezone(timedelta(hours=9))
        return jsonify(
            rows=rows, candidates=candidates, counts=counts,
            win_odds=sorted([{'horse':h,'odds':o} for h,o in W], key=lambda x:x['odds']),
            fetched_at=datetime.now(jst).isoformat(timespec='seconds'),
            version='0.4'
        )
    except Exception as e:
        return jsonify(error=str(e)), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8787, debug=False)
