#!/usr/bin/env python3
"""
scripts/bench_llm.py -- local LLM benchmark for CloudOps alert summaries (AI/ML audit Phase 1).

Compares Ollama models on the EXACT prompt production sends (app/llm/summarizer.py's
system prompt + _build_summary_user_content), using built-in synthetic alert facts. It reads
no database, writes nothing, and makes no AWS calls.

Reports per model: load time, tokens/second, total latency, and how many outputs pass the
deterministic grounding check (ungrounded_tokens) -- i.e. quality AND speed.

  python3 scripts/bench_llm.py --models llama3.2:3b llama3.2:1b qwen2.5:3b
  python3 scripts/bench_llm.py --models llama3.2:3b --repeat 1 --show

WARNING: this saturates the CPU while it runs. Do NOT run it on PROD while the EC2 CPU credit
balance is at zero (the app and MySQL share those 2 vCPUs). Run it on DEV (no monitoring load)
or on PROD only off-peak with a healthy credit balance. A model must already be pulled
(`ollama pull <name>`); this script never pulls anything.
"""
import argparse
import json
import os
import statistics
import sys
import time

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from app.llm import summarizer as S  # noqa: E402

FIXTURES = [
    ({"confidence": "high", "trend": "rising steadily over the last 2 hours", "is_likely_flapping": False,
      "probable_trigger": {"event_name": "ModifyInstanceAttribute", "username": "deploy-bot",
                           "event_time": "2026-09-30 05:41:10"},
      "related_alert_count": 2},
     "CPUUtilization on web-prod-01 (i-0a1b2c3d4e) reached 91.3% against an 80% threshold. A related AWS "
     "change, ModifyInstanceAttribute by deploy-bot at 2026-09-30 05:41:10, happened shortly before. "
     "2 other alerts are firing alongside this one."),
    ({"confidence": "low", "trend": "sudden spike in the last 15 minutes", "is_likely_flapping": True,
      "probable_trigger": None, "related_alert_count": 0},
     "NetworkOut on app-prod-02 spiked to 5200000 bytes against a 1000000 threshold. No related AWS activity "
     "was found; this alert has opened and closed 6 times today and may be normal variation."),
    ({"confidence": "medium", "trend": "gradual climb over 2 hours", "is_likely_flapping": False,
      "probable_trigger": None, "related_alert_count": 1},
     "FreeStorageSpace on rds-orders-prod fell to 12.4 GB, below the 20 GB threshold, and has dropped steadily "
     "since 04:00. 1 other alert is firing on a dependent resource."),
    ({"confidence": "high", "trend": "flat", "is_likely_flapping": False,
      "probable_trigger": {"event_name": "PutScalingPolicy", "username": "alice",
                           "event_time": "2026-10-01 09:12:44"},
      "related_alert_count": 4},
     "HTTPCode_Target_5XX_Count on alb-prod-main reached 118 against a threshold of 50. 4 other alerts, "
     "including its target instances, started within 30 minutes. PutScalingPolicy by alice at "
     "2026-10-01 09:12:44 was the nearest AWS change."),
]


def run_one(host, model, system_prompt, user_content, num_predict, num_ctx, timeout):
    t0 = time.time()
    r = requests.post(f"{host}/api/chat", timeout=timeout, json={
        "model": model, "stream": False, "think": False,
        "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_content}],
        "options": {"num_predict": num_predict, "num_ctx": num_ctx, "temperature": 0.2},
    })
    r.raise_for_status()
    d = r.json()
    wall = time.time() - t0
    eval_s = (d.get("eval_duration") or 0) / 1e9
    toks = d.get("eval_count") or 0
    return {
        "text": ((d.get("message") or {}).get("content") or "").strip(),
        "load_s": (d.get("load_duration") or 0) / 1e9,
        "tok_s": (toks / eval_s) if eval_s else 0.0,
        "tokens": toks,
        "wall_s": wall,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", nargs="+", default=["llama3.2:3b"])
    ap.add_argument("--host", default=os.getenv("OLLAMA_HOST", "http://localhost:11434"))
    ap.add_argument("--repeat", type=int, default=1, help="passes over the fixtures per model")
    ap.add_argument("--num-predict", type=int, default=S._DEFAULT_MAX_TOKENS)
    ap.add_argument("--num-ctx", type=int, default=S._DEFAULT_NUM_CTX)
    ap.add_argument("--timeout", type=float, default=300)
    ap.add_argument("--show", action="store_true", help="print every generated paragraph")
    args = ap.parse_args()

    host = args.host.rstrip("/")
    rows = []
    for model in args.models:
        print(f"\n== {model} ==", flush=True)
        try:  # warm-up so load time is not billed to the first fixture
            run_one(host, model, "Reply OK.", "OK", 4, args.num_ctx, args.timeout)
        except Exception as e:
            print(f"  skipped ({e}) -- is the model pulled?")
            continue
        results = []
        for _ in range(args.repeat):
            for facts, template in FIXTURES:
                content = S._build_summary_user_content(facts, template)
                res = run_one(host, model, S._SUMMARY_SYSTEM_PROMPT, content,
                              args.num_predict, args.num_ctx, args.timeout)
                bad = S.ungrounded_tokens(res["text"], json.dumps(facts, default=str), template)
                res["bad"] = bad
                results.append(res)
                print(f"  {res['wall_s']:5.1f}s  {res['tok_s']:4.1f} tok/s  {res['tokens']:3d} tok  "
                      f"{'GROUNDED' if res['text'] and not bad else 'REJECT ' + str(bad[:3])}", flush=True)
                if args.show:
                    print(f"    {res['text']}")
        ok = sum(1 for r_ in results if r_["text"] and not r_["bad"])
        rows.append((model, statistics.median(r_["wall_s"] for r_ in results),
                     statistics.median(r_["tok_s"] for r_ in results), ok, len(results)))

    print("\nmodel                median_s  median_tok/s  grounded")
    for model, wall, tps, ok, n in rows:
        print(f"{model:<20} {wall:8.1f}  {tps:12.1f}  {ok}/{n}")
    print("\nPick the fastest model whose grounded rate stays at or near the baseline's; read a few with --show.")


if __name__ == "__main__":
    main()
