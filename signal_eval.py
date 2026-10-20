"""シグナル等級（S〜D）ごとの成績。入出力なしの純粋関数。

各等級の馬を複勝・単勝で100円ずつ買った場合の回収率と、3着内率を出す。
「買い」と判定するのは、発走前に記録した判定（今後の記録）だけで、
複勝回収率の95%区間の下限が100%を超えた等級に限る。過去分の再計算は参考値。"""
from roi import _summary

GRADES = ("S", "A", "B", "C", "D")
MIN_BUY_HORSES = 100


def _grade_block(rows):
    n = len(rows)
    place = _summary([(100, int(r.get("place_yen") or 0)) for r in rows])
    win = _summary([(100, int(r.get("win_yen") or 0)) for r in rows])
    pops = [int(r["pop"]) for r in rows if r.get("pop")]
    return {"horses": n, "placed": sum(1 for r in rows if r["placed"]),
            "place_rate": round(sum(1 for r in rows if r["placed"]) / n, 4) if n else None,
            "place_roi": place["roi"], "place_ci": place["ci95"], "win_roi": win["roi"],
            "avg_pop": round(sum(pops) / len(pops), 1) if pops else None}


def verdict(block, prospective):
    if not prospective:
        return "参考"
    if block["horses"] < MIN_BUY_HORSES:
        return "データ不足"
    ci = block.get("place_ci")
    if ci and ci[0] > 1.0:
        return "買い候補"
    if block.get("place_roi") is not None and block["place_roi"] < 1.0 and ci and ci[1] < 1.0:
        return "見送り"
    return "判定保留"


def evaluate(rows, prospective):
    """rows: [{"race_key","horse","rank","grade","pop","placed","place_yen","win_yen"}]"""
    races = {r["race_key"] for r in rows}
    by_grade, top1 = [], []
    for g in GRADES:
        b = _grade_block([r for r in rows if r["grade"] == g])
        b.update(grade=g, verdict=verdict(b, prospective) if b["horses"] else "—")
        by_grade.append(b)
        t = _grade_block([r for r in rows if r["grade"] == g and int(r.get("rank") or 99) == 1])
        t.update(grade=g)
        top1.append(t)
    return {"races": len(races), "horses": len(rows), "by_grade": by_grade, "top1": top1,
            "min_buy_horses": MIN_BUY_HORSES, "prospective": prospective}


def result_lookup(result_row):
    """results の行から (3着内の馬の集合, 複勝払戻 {馬:円}, 単勝払戻 {馬:円}, 払戻あり?)"""
    import json
    top = {result_row["first_horse"], result_row["second_horse"], result_row["third_horse"]}
    place, win, priced = {}, {}, False
    try:
        pl = json.loads(result_row.get("payload") or "null")
        if isinstance(pl, dict):
            if pl.get("order") and pl.get("places"):
                top = {int(h) for h, p in zip(pl["order"], pl["places"]) if int(p) <= 3}
            pay = pl.get("payouts") or {}
            if pay.get("複勝"):
                priced = True
                place = {int(e["combo"][0]): int(e["yen"]) for e in pay["複勝"]}
                win = {int(e["combo"][0]): int(e["yen"]) for e in pay.get("単勝", [])}
    except Exception:
        pass
    return top, place, win, priced
