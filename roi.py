"""Return-on-investment evaluation from official payouts. Pure functions; no I/O.

Every strategy buys 100 yen per ticket. "SCOPE" uses the stored FINAL top-3 picks, "人気" uses the
three lowest win odds at the same stage, on exactly the same races, so the two are comparable.
"""
import random
from itertools import combinations

STRATEGIES = (("単勝", "win"), ("複勝", "place"), ("ワイドBOX", "wide"), ("馬連BOX", "quinella"), ("3連複", "trio"))
LATE_BUCKETS = (("急に売れた", None, -0.20), ("売れた", -0.20, -0.05), ("横ばい", -0.05, 0.05), ("売れなかった", 0.05, None))


def payout_index(payouts):
    idx = {}
    for kind, entries in (payouts or {}).items():
        for e in entries:
            combo = tuple(e["combo"])
            if kind in ("ワイド", "馬連複", "三連複", "枠連複"):
                combo = tuple(sorted(combo))
            idx[(kind, combo)] = idx.get((kind, combo), 0) + int(e["yen"])
    return idx


def tickets(horses, strategy):
    hs = [int(h) for h in horses][:3]
    if strategy == "win":
        return [("単勝", (h,)) for h in hs]
    if strategy == "place":
        return [("複勝", (h,)) for h in hs]
    if strategy == "wide":
        return [("ワイド", tuple(sorted(p))) for p in combinations(hs, 2)]
    if strategy == "quinella":
        return [("馬連複", tuple(sorted(p))) for p in combinations(hs, 2)]
    if strategy == "trio":
        return [("三連複", tuple(sorted(hs)))] if len(hs) == 3 else []
    raise ValueError(strategy)


def race_outcome(horses, payouts, strategy):
    idx = payout_index(payouts)
    t = tickets(horses, strategy)
    ret = sum(idx.get(x, 0) for x in t)
    return 100 * len(t), ret


def _summary(per_race, seed=7, n_boot=1000):
    stake = sum(s for s, _ in per_race)
    ret = sum(r for _, r in per_race)
    hits = sum(1 for _, r in per_race if r > 0)
    out = {"races": len(per_race), "stake": stake, "return": ret, "roi": round(ret / stake, 4) if stake else None,
           "hit_rate": round(hits / len(per_race), 4) if per_race else None, "ci95": None}
    if len(per_race) >= 20 and stake:
        rng = random.Random(seed)
        vals = []
        for _ in range(n_boot):
            sample = [per_race[rng.randrange(len(per_race))] for _ in per_race]
            s = sum(x for x, _ in sample)
            vals.append(sum(y for _, y in sample) / s if s else 0.0)
        vals.sort()
        out["ci95"] = [round(vals[int(0.025 * n_boot)], 4), round(vals[int(0.975 * n_boot) - 1], 4)]
    return out


def evaluate(races):
    """races: [{"race_key", "picks": [h,h,h], "favorites": [h,h,h], "payouts": {...}}]."""
    usable = [r for r in races if len(r.get("picks") or []) >= 3 and len(r.get("favorites") or []) >= 3 and "単勝" in (r.get("payouts") or {})]
    out = {"races": len(usable), "strategies": []}
    for label, key in STRATEGIES:
        scope = [race_outcome(r["picks"], r["payouts"], key) for r in usable]
        fav = [race_outcome(r["favorites"], r["payouts"], key) for r in usable]
        out["strategies"].append({"label": label, "key": key, "scope": _summary(scope), "favorite": _summary(fav)})
    return out


def late_money(horses):
    """horses: [{"odds3", "final", "placed": bool, "place_yen": int}] -> stats per 3分前→確定 change bucket.
    Buckets mix popularity levels; read them as a first look, not as proof."""
    out = []
    for label, lo, hi in LATE_BUCKETS:
        rows = []
        for h in horses:
            if not h.get("odds3") or not h.get("final"):
                continue
            ch = h["final"] / h["odds3"] - 1
            if (lo is None or ch > lo) and (hi is None or ch <= hi):
                rows.append(h)
        n = len(rows)
        placed = sum(1 for h in rows if h["placed"])
        ret = sum(h.get("place_yen", 0) for h in rows)
        out.append({"label": label, "horses": n, "place_rate": round(placed / n, 4) if n else None,
                    "place_roi": round(ret / (100 * n), 4) if n else None})
    return out
