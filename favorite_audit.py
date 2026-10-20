"""1倍台の人気馬（3分前の単勝2.0倍未満）が3着を外す兆候があったかを、保存済みデータで検証する。
入出力なしの純粋関数。判定は事前に取得したオッズだけを使い、結果は答え合わせにだけ使う。"""

HEAVY = 2.0           # 3分前の単勝オッズがこれ未満を「1倍台の人気馬」とする
WEAK_BASE = -0.5      # 他券種の支持（base）がこれ以下＝単勝ほど売れていない
STRONG_BASE = 0.5
DRIFT = 0.10          # 15分前→3分前で単勝オッズが10%以上上昇＝売られていない


def signals(fav):
    """fav: {"odds3","odds15"(任意),"base3"} -> (判定, 理由リスト)"""
    doubts, trusts = [], []
    b = fav.get("base3")
    if b is not None and b <= WEAK_BASE:
        doubts.append("馬連・3連複などでは単勝ほど支持されていない")
    elif b is not None and b >= STRONG_BASE:
        trusts.append("他の券種でも単勝以上に支持されている")
    o15, o3 = fav.get("odds15"), fav.get("odds3")
    if o15 and o3:
        ch = o3 / o15 - 1
        if ch >= DRIFT:
            doubts.append(f"15分前から単勝オッズが上昇（{o15:.1f}→{o3:.1f}倍）")
        elif ch <= -DRIFT:
            trusts.append(f"15分前から単勝オッズが下落（{o15:.1f}→{o3:.1f}倍）")
    if len(doubts) >= 2:
        verdict = "危険"
    elif doubts:
        verdict = "注意"
    elif trusts:
        verdict = "信頼"
    else:
        verdict = "普通"
    return verdict, doubts + trusts


def audit(rows):
    """rows: [{"race_key","odds3","odds15","base3","placed":bool,"place_yen":int|None}]"""
    groups = {}
    for r in rows:
        v, _ = signals(r)
        g = groups.setdefault(v, {"verdict": v, "horses": 0, "placed": 0, "place_return": 0, "priced": 0})
        g["horses"] += 1
        g["placed"] += int(bool(r["placed"]))
        if r.get("place_yen") is not None:
            g["priced"] += 1
            g["place_return"] += int(r["place_yen"] or 0)
    out = []
    for v in ("信頼", "普通", "注意", "危険"):
        g = groups.get(v, {"verdict": v, "horses": 0, "placed": 0, "place_return": 0, "priced": 0})
        g["place_rate"] = round(g["placed"] / g["horses"], 4) if g["horses"] else None
        g["place_roi"] = round(g["place_return"] / (100 * g["priced"]), 4) if g["priced"] else None
        out.append(g)
    total = len(rows)
    return {"heavy_favorites": total, "placed": sum(int(bool(r["placed"])) for r in rows),
            "place_rate": round(sum(int(bool(r["placed"])) for r in rows) / total, 4) if total else None, "by_verdict": out}
