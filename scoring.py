"""ODDS SCOPE NAR Ver.11 — scoring helpers.

The live application performs the full calculation in app.py so that market
snapshots, NAR profiles and validation are evaluated together.  This module
keeps the public scoring concepts explicit and reusable.
"""
import math

FRAME_COLORS={1:"white",2:"black",3:"red",4:"blue",5:"yellow",6:"green",7:"orange",8:"pink"}
DNA_WEIGHTS={"gap":.12,"isolation":.16,"float":.14,"cross":.18,"potun":.22,"accel":.18}

def clamp(v,lo=0.0,hi=100.0):
    return max(lo,min(hi,float(v)))

def weighted_available(items):
    vals=[(float(v),float(w)) for v,w in items if v is not None]
    if not vals:return None
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
    if edge is None:return round(clamp(.88*market+.12*value),1)
    return round(clamp(.70*market+.25*float(edge)+.05*value),1)

def scope_label(scope,dna,flow,edge=None,value=None):
    scope=float(scope or 0);dna=float(dna or 0);flow=float(flow or 0);value=float(value or 0)
    if scope>=82 and dna>=80 and flow>=62 and (edge is None or edge>=54):return "HOT"
    if scope>=75 and dna>=68 and flow>=55 and (edge is None or edge>=50):return "CORE"
    if value>=70 and dna>=58:return "VALUE"
    if scope>=64 or dna>=62:return "WATCH"
    return "NEUTRAL"
