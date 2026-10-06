"""ODDS SCOPE NAR Ver.13 — reusable scoring helpers.

The live application performs the full market, profile, time-series, RACE FLOW,
result calibration and adaptive learning calculation in app.py. This module
keeps the public scoring concepts small and explicit and never fabricates
missing inputs.
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
    """Market-first heuristic. RACE FLOW is intentionally not double-counted here.

    RACE FLOW is a corroborating filter and an adaptive-learning feature; pace is
    already present inside EDGE when official profile data is available.
    """
    dna=float(dna or 0);flow=float(flow or 0);value=float(value or 0)
    market=.74*dna+.26*flow
    if edge is None:
        return round(clamp(.88*market+.12*value),1)
    return round(clamp(.70*market+.25*float(edge)+.05*value),1)


def race_flow_score(style=None,frame=None,front_count=0):
    if style=="逃・先":base=72.0 if front_count<=2 else (58.0 if front_count==3 else 43.0)
    elif style=="先・好位":base=64.0 if front_count<=3 else 70.0
    elif style=="中団":base=56.0 if front_count<=3 else 64.0
    elif style=="差・追":base=48.0 if front_count<=2 else (60.0 if front_count==3 else 70.0)
    else:return None
    if style in ("逃・先","先・好位"):
        if frame in (1,2,3):base+=3
        elif frame in (7,8):base-=2
    elif style=="差・追":
        if front_count>=4:base+=4
        elif front_count<=2:base-=3
    return round(clamp(base,20,85),1)


def signal_level(scope,dna,flow,accel,potun,cross,edge=None,race_flow=None,pop=99,data_confidence=100):
    """Conservative evidence-alignment grade (S/A/B/C/D), not win probability."""
    scope=float(scope or 0);dna=float(dna or 0);flow=float(flow or 0);accel=float(accel or 0)
    potun=float(potun or 0);cross=float(cross or 0);edgev=50.0 if edge is None else float(edge)
    rf=50.0 if race_flow is None else float(race_flow)
    confirmations=sum((dna>=55,flow>=55,accel>=62,potun>=58,cross>=60,(edge is not None and edgev>=57),rf>=64))
    idx=.31*scope+.14*dna+.13*flow+.11*accel+.08*potun+.07*cross+.08*edgev+.08*rf+max(0,confirmations-1)*1.8
    if int(pop or 99)>=7 and confirmations<2:idx-=5
    if int(pop or 99)>=10 and confirmations<3:idx-=3
    if int(pop or 99)<=3 and edge is not None and edgev>=58 and flow>=38:idx+=2
    if float(data_confidence or 0)<50:idx-=4
    idx=clamp(idx)
    if idx>=72 and confirmations>=4:grade,label="S","PRIME"
    elif idx>=62 and confirmations>=3:grade,label="A","STRONG"
    elif idx>=52 and confirmations>=1:grade,label="B","SELECT"
    elif idx>=42:grade,label="C","WATCH"
    else:grade,label="D","LOW"
    return {"index":round(idx,1),"grade":grade,"label":label,"confirmations":confirmations}
