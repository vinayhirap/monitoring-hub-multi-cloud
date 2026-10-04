# Ollama on CloudOps -- setup, sizing and operations

Written 2026-10-03 from the AI/ML audit. Until now none of this lived in the repo (`setup.sh`,
`update.sh` and `deploy/` never mentioned Ollama), so PROD and DEV could not be rebuilt from it.
Numbers marked MEASURED come from PROD (t3.large, 2 vCPU / 8 GiB, CPU credits exhausted at the
time); anything else is a recommendation, not a measurement.

## What the app uses it for (and does not)

Ollama only rewrites facts the app has already computed deterministically:

| Use | Code | Output cap | Timeout |
|---|---|---|---|
| Alert summary paragraph | `app/collector/llm_summarizer.py` -> `polish_summary` | 160 tokens | `LLM_SUMMARY_TIMEOUT_SECONDS` |
| RCA report narrative (cached) | `app/llm/rca_report.py` -> `generate_rca_narrative` | 320 tokens | `LLM_RCA_TIMEOUT_SECONDS` (180) |

Every output is checked by `ungrounded_tokens()` (numbers/IDs/URLs must exist in the input facts)
and falls back to the deterministic template if not. Detection, correlation, baselines,
forecasting and NL search never call it.

## Install (fresh host)

```bash
curl -fsSL https://ollama.com/install.sh | sh        # creates the `ollama` user and ollama.service
ollama pull llama3.2:3b                              # ~2.0 GB on disk
```

App side, in `.env` (OFF by default):

```
LLM_SUMMARY_ENABLED=true
LLM_PROVIDER=ollama
OLLAMA_HOST=http://localhost:11434
OLLAMA_MODEL=llama3.2:3b
```

## Recommended service settings

`OLLAMA_HOST` means two different things: in `.env` it is the URL the *app* calls; in the systemd
unit it is the address the *server* binds to. Create `/etc/systemd/system/ollama.service.d/override.conf`:

```ini
[Service]
Environment="OLLAMA_HOST=127.0.0.1:11434"
Environment="OLLAMA_NUM_PARALLEL=1"
Environment="OLLAMA_MAX_LOADED_MODELS=1"
```

- `127.0.0.1` keeps the API off the network (nothing else needs it).
- `NUM_PARALLEL=1` and `MAX_LOADED_MODELS=1`: on 2 vCPUs a second concurrent generation just halves
  both speeds, and a second resident model would not fit next to the app and MySQL.
- Keep-alive is deliberately left at Ollama's default (5 min). The summary job runs every 15 min, so the
  model unloads between runs and returns ~2.4 GB to the app; reload from page cache costs seconds.
  Set `OLLAMA_KEEP_ALIVE` in the app's `.env` only if measurements show cold loads hurting.

Apply (restarts Ollama only -- not monitoring-hub, not the instance):

```bash
sudo systemctl daemon-reload && sudo systemctl restart ollama
systemctl is-active ollama && ollama ps
```

Verify the override took effect: `systemctl show ollama -p Environment`.

## Measured behaviour (PROD, 2026-10-02)

- `llama3.2:3b`, 4-bit: 2.0 GB on disk, ~2.4-2.6 GB resident, context 3072 after Phase 1 (was 4096).
- ~3.5 tokens/s on 2 throttled vCPUs. Output length is therefore latency: 160 tokens ~ 45 s,
  320 tokens ~ 90 s.
- A 7-8B model needs ~5 GB and would run at a fraction of that speed: not viable on this box.

## Choosing or changing the model

1. Benchmark on the real prompt: `python3 scripts/bench_llm.py --models llama3.2:3b llama3.2:1b qwen2.5:3b --show`
   (synthetic facts, no DB, no AWS). It saturates the CPU: never while the CPU credit balance is near zero.
   The 3B model does not fit beside the app on the 4 GiB DEV box -- benchmark the 1b model there.
2. Compare speed AND the `grounded` column (share of outputs that pass the verifier).
3. Change `OLLAMA_MODEL` in `.env`, `ollama pull <model>`, restart monitoring-hub.
4. Rollback: put the old value back and restart. Old weights stay on disk until `ollama rm`.

`llama3.2:1b` (1.3 GB) is installed on PROD but unused; remove it with `ollama rm llama3.2:1b`
once the benchmark decision is made.

## Model drift: refresh and pin

The app re-pulls the configured tag once a day. If the publisher moves that tag, the weights change
with no deploy and no review. Since 2026-10-03 the log says so:

```
journalctl -u monitoring-hub | grep "llm_summarizer.*refreshed Ollama model"     # shows (digest xxxxxxxxxxxx)
journalctl -u monitoring-hub | grep "CHANGED upstream"                           # WARNING when it moves
```

Once a model is validated, freeze it: `OLLAMA_AUTO_REFRESH=false` in `.env` and restart. The current
digest is also visible with `ollama list` (the ID column). To take an update deliberately, set it back
to `true` (or run `ollama pull <model>` by hand), review a few summaries, then pin again.

## Health checks

```bash
ollama --version; ollama list; ollama ps                       # loaded model, size, context
free -m                                                         # "available" should stay above ~2 GB
journalctl -u monitoring-hub --since "1 hour ago" -q | grep -E "llm_summarizer|rca_report|rejected"
```

- `rejected polished summary` / `rejected RCA narrative` lines name the offending tokens: tune from those.
- `90s budget reached` repeatedly means more alerts than the 3B model can summarise per cycle.
- `rca_narratives` holds cached RCA narratives (migration 076); rows can be deleted safely, they regenerate.
