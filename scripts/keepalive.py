#!/usr/bin/env python3
"""Keep the Render Free service awake during NAR race hours and audit capture health.

Render Free stops a web service after 15 minutes without inbound HTTP. The in-process 15-second
scheduler cannot wake itself, so an outside caller must hit the service at least that often.
This script pings /api/tick every 60 s and every 10 minutes prints /api/capture-health.
It exits non-zero (GitHub sends a failure e-mail) when the service is unreachable for several
minutes in a row or when slots were missed because the scheduler was not running.

Standard library only. Usage: python3 scripts/keepalive.py [--once]
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
BASE = os.environ.get("ODDS_SCOPE_URL", "https://potun-auto.onrender.com").rstrip("/")
START_H, END_H = 8, 22          # JST window covering NAR day and night meetings
PING_EVERY = 60                 # seconds; Render sleeps after 15 min idle
REPORT_EVERY = 600              # seconds
MAX_RUN = int(os.environ.get("KEEPALIVE_MAX_SECONDS", str(5 * 3600 + 45 * 60)))
FAIL_FAST_AFTER = 5             # consecutive failed pings (~5 min down) -> fail the job now
SCHEDULER_CAUSES = ("SCHEDULER_NOT_RUNNING", "NOT_ATTEMPTED")


def get(path, timeout=100):
    req = urllib.request.Request(BASE + path, headers={"User-Agent": "odds-scope-keepalive/1", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8"))


def report(seen):
    """Print today's audit; return slot misses newly attributable to the scheduler."""
    status, body = get("/api/capture-health")
    s, sch = body.get("summary", {}), body.get("scheduler", {})
    print(f"[health] {body.get('now')} slots_ok={s.get('slots_ok')}/{s.get('slots_due')} rate={s.get('success_rate')} "
          f"missed={s.get('missed_by_reason')} beat_age={sch.get('beat_age_s')}s stale={sch.get('stale')} workers={sch.get('capture_workers')}", flush=True)
    bad = []
    for race in body.get("races", []):
        line = " ".join(f"{x['slot']}:{'OK' if x['status'] == 'OK' else x.get('reason', x['status'])}" for x in race["slots"])
        print(f"  {race['race_key']} {race.get('baba_name') or ''}{race.get('race')}R {race['start_iso'][11:16]} [{line}] "
              f"samples={race.get('learning_samples')}", flush=True)
        for x in race["slots"]:
            k = (race["race_key"], x["slot"])
            if x["status"] == "MISSED" and k not in seen:
                seen.add(k)
                level = "error" if x.get("reason") in SCHEDULER_CAUSES else "warning"
                print(f"::{level}::{race['race_key']} {x['slot']}分前 未取得 {x.get('reason')} {x.get('reason_ja', '')} {x.get('detail', '')}", flush=True)
                if x.get("reason") in SCHEDULER_CAUSES:
                    bad.append(k)
    if sch.get("stale"):
        print(f"::error::scheduler heartbeat stale ({sch.get('beat_age_s')}s)", flush=True)
    return bad


def main():
    once = "--once" in sys.argv
    began = time.time()
    now = datetime.now(JST)
    if not once and not (START_H <= now.hour < END_H):
        print(f"outside race hours ({now:%H:%M} JST); nothing to do")
        return 0
    end_today = now.replace(hour=END_H, minute=0, second=0, microsecond=0)
    seen, scheduler_misses = set(), []
    fails = worst = 0
    last_report = 0.0
    # Slots that were already missed before this run started are reported once, not blamed on this run.
    try:
        report(seen)
        last_report = time.time()
    except Exception as exc:
        print(f"::warning::initial health check failed: {exc}", flush=True)
    seen_at_start = set(seen)
    while True:
        t0 = time.time()
        try:
            status, body = get("/api/tick")
            sch = body.get("scheduler", {})
            fails = 0
            print(f"[tick] {datetime.now(JST):%H:%M:%S} http={status} started={body.get('started')} loop_alive={sch.get('loop_alive')} "
                  f"beat={str(sch.get('loop_beat'))[11:19]} pid={sch.get('pid')} up_since={str(sch.get('process_started'))[11:19]}", flush=True)
        except Exception as exc:
            fails += 1
            worst = max(worst, fails)
            print(f"::warning::ping failed ({fails} in a row): {type(exc).__name__}: {exc}", flush=True)
            if fails >= FAIL_FAST_AFTER:
                print(f"::error::service unreachable for {fails} consecutive minutes", flush=True)
                return 1
        if once:
            report(seen)
            return 0 if fails == 0 else 1
        if time.time() - last_report >= REPORT_EVERY:
            try:
                scheduler_misses += [k for k in report(seen) if k not in seen_at_start]
            except Exception as exc:
                print(f"::warning::health check failed: {exc}", flush=True)
            last_report = time.time()
        if time.time() - began >= MAX_RUN or datetime.now(JST) >= end_today:
            break
        time.sleep(max(1.0, PING_EVERY - (time.time() - t0)))
    try:
        scheduler_misses += [k for k in report(seen) if k not in seen_at_start]
    except Exception as exc:
        print(f"::warning::final health check failed: {exc}", flush=True)
    if scheduler_misses:
        print(f"::error::{len(scheduler_misses)} slot(s) missed during this run because the scheduler was not running: {scheduler_misses}")
        return 1
    print(f"run finished; worst consecutive ping failures={worst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
