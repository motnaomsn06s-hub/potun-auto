"""ODDS SCOPE NAR Ver.12 — reusable scoring helpers.

The live application performs the full market, profile, time-series and learning
calculation in app.py.  This module documents the public scoring concepts used by
that application without fabricating missing inputs.
"""

FRAME_COLORS={1:"white",2:"black",3:"red",4:"blue",5:"yellow",6:"green",7:"orange",8:"pink"}
DNA_WEIGHTS={"gap":.12,"isolation":.16,"float":.14,"cross":.18,"potun":.22,"accel":.18}


def clamp(v,lo=0.0,hi=100.0):
    return max(lo,min(hi,float(v)))


def weighted_available(items):
    vals=[(float(v),float(w)) for v,w in items if v is not None]
    if not vals:
        return None
    return sum(v*w for v,w in vals)/sum(w for _,w in vals)


def odds_dna_score(gap=None,isolation=None,float_score=None,cross=None,potun=None,accel=None):
    parts=[(gap,DNA_WEIGHTS["gap"]),(isolation,DNA_WEIGHTS["isolation"]),(float_score,DNA_WEIGHTS["float"]),
           (cross,DNA_WEIGHTS["cross"]),(potun,DNA_WEIGHTS["potun"]),(accel,DNA_WEIGHTS["accel"])]
    score=weighted_available(parts)
    return None if score is None else round(clamp(score),1)


def edge_score(form=None,distance=None,course=None,jockey=None,pace=None,condition=None):
    score=weighted_available([(form,.35),(distance,.20),(course,.15),(jockey,.10),(pace,.10),(condition,.10)])
    return None if score is None else round(clamp(score),1)


def scope_score(dna,flow,edge=None,value=None):
    dna=float(dna or 0);flow=float(flow or 0);value=float(value or 0)
    market=.74*dna+.26*flow
    if edge is None:
        return round(clamp(.88*market+.12*value),1)
    return round(clamp(.70*market+.25*float(edge)+.05*value),1)


def signal_level(scope,dna,flow,accel,potun,cross,edge=None,pop=99,data_confidence=100):
    """Return a conservative evidence-alignment grade (S/A/B/C/D).

    This is not a win probability.  It exists so a ranking position is not
    misrepresented as a strong signal when the underlying evidence is weak.
    """
    scope=float(scope or 0);dna=float(dna or 0);flow=float(flow or 0);accel=float(accel or 0)
    potun=float(potun or 0);cross=float(cross or 0);edgev=50.0 if edge is None else float(edge)
    confirmations=sum((dna>=58,flow>=58,accel>=65,potun>=60,cross>=65,(edge is not None and edgev>=58)))
    idx=.34*scope+.15*dna+.14*flow+.12*accel+.09*potun+.08*cross+.08*edgev+max(0,confirmations-1)*2
    if int(pop or 99)>=7 and confirmations<2:idx-=4
    if int(pop or 99)>=10 and confirmations<3:idx-=2
    if int(pop or 99)<=3 and edge is not None and edgev>=58 and flow>=40:idx+=2.5
    if float(data_confidence or 0)<50:idx-=4
    idx=clamp(idx)
    if idx>=72 and confirmations>=3:grade,label="S","PRIME"
    elif idx>=62 and confirmations>=2:grade,label="A","STRONG"
    elif idx>=53:grade,label="B","SELECT"
    elif idx>=44:grade,label="C","WATCH"
    else:grade,label="D","LOW"
    return {"index":round(idx,1),"grade":grade,"label":label,"confirmations":confirmations}
