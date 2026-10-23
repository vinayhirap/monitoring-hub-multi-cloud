# Host agent channel: threat model

Scope: the push channel between `cloudops-agent` (on servers) and the hub: `POST /api/host-agents/enroll` and `POST /api/host-agents/ingest`, plus the admin routes that mint and revoke credentials. Not the AI agent.

## Review basis (read this first)

| Part | State when written | How reviewed |
|---|---|---|
| Phase B: enroll, enrollment keys, tokens, `authenticate_agent`, revoke, config, list | **merged** (`1a723c3`) | against the actual code and its tests |
| Phase C: ingest handler | **not merged**. `POST /api/host-agents/ingest` is a 501 stub that authenticates nothing | against the **CONTRACT** only. Rows marked `CONTRACT` are requirements phase C must meet, each with the test that must exist |
| Phase D (state/UI), E (agent binary, `agent/`) | **not merged** (no `agent/` directory) | CONTRACT only |
| Phase F: nginx, retention | this patch | actual code and tests |

Re-run this review when C and E land: every `CONTRACT` row below becomes a code reference or a gap.

## Assets and trust boundaries

Agent token (`cah_`, 40 url-safe chars) and enrollment key (`cak_`, 32 chars) are bearer secrets; only SHA-256 is stored. Metrics are low-value data but drive alerts, so integrity matters more than confidentiality. The hub is the trust root: it serves the agent binary and controls agent config (see distribution doc). The agent holds no inbound port.

## Threats

Legend for "Where": file in repo; `CONTRACT` = not yet in repo.

### 1. Stolen agent token

| | |
|---|---|
| Impact | Attacker can push fake samples for **that one host** (alert poisoning, hiding a real outage) until revoked. Cannot read any data: ingest returns only `{accepted, rejected, config_version, config?}` for the caller's own host. Cannot reach other hosts or accounts. |
| Controls | High-entropy token, hash at rest (a DB read does not yield usable tokens); token bound to one host and one account (`authenticate_agent` returns `aws_account_id`/`host_agent_id`; ingest must use these and ignore any identity in the payload); HTTPS only; admin revoke kills all tokens and sets host `revoked`; re-enroll rotates the token; per-token ingest rate limit. |
| Where | `app/hostagents/auth.py` (`generate_token`, `hash_secret`, `authenticate_agent`: constant-time `hmac.compare_digest`, rejects revoked token or revoked host, never writes); `app/api/host_agents.py` (`revoke_host_agent`, `enroll` rotation); per-token limit `HOST_AGENT_INGEST_RATE_PER_MIN` = `CONTRACT`. |
| Tests | `test_authenticate_agent_rejects_unknown_malformed_revoked_and_disabled`, `test_authenticate_agent_uses_constant_time_compare`, `test_revoke_host_revokes_all_tokens_blocks_auth_and_is_idempotent`, `test_re_enroll_same_account_and_hostname_rotates_token_and_keeps_admin_config`, `test_enroll_returns_token_once_and_stores_only_hashes`. Needed in C: ingest ignores a payload `host.hostname` that differs from the token's host; per-token 429. |
| Gaps | (a) Tokens never expire or auto-rotate. (b) No anomaly detection for a token used from a second IP. (c) A stolen token cannot be told apart from the real agent (both push) until `seq` conflicts appear. Mitigation today: revoke, re-enroll. |

### 2. Replayed payloads

| | |
|---|---|
| Impact | Duplicate rows, or stale values re-asserted as current. |
| Controls | TLS (capture requires breaking TLS); `seq` monotonic per host, server treats `seq <= last_seq` within 10 minutes as duplicate (restart reset allowed); sample `ts` window (future > 10 min and past > 24 h are dropped and counted as rejected); token binds payload to one host. |
| Where | **CONTRACT** (phase C ingest). Enroll resets `last_seq = 0` (`app/api/host_agents.py`, upsert). |
| Tests | Needed in C: same payload twice → second `accepted = 0`; `seq` lower after > 10 min accepted as restart; `ts` boundaries (+10 min, −24 h). |
| Gaps | (a) A replay older than 10 min with a lower `seq` looks like a restart and is accepted if its samples are within 24 h. (b) `metric_history` has no unique key on `(resource_id, metric_name, metric_timestamp)`, so an accepted duplicate becomes a second row, unless C deduplicates. Both need TLS to be broken first, so priority is low. |

### 3. Hostile payloads / metric-name injection

| | |
|---|---|
| Impact | SQL/log injection, unbounded metric cardinality (table bloat), XSS via names rendered in the UI, memory exhaustion. |
| Controls | Exact allowlist of v1 metric names; label keys limited to `mount` and `iface`; `schema` must be 1; values finite (NaN/Inf rejected); `max_batch_metrics` 500; body cap; services names validated with the same regex as the admin config; every SQL statement uses bound parameters; hostname/OS/arch/version validated at enroll. |
| Where | Enroll fields: `app/api/host_agents.py` `EnrollRequest` (patterns + max lengths). Config service names: `_SERVICE_RE`. Payload validation: **CONTRACT** (phase C). nginx caps the body (`client_max_body_size 256k`). |
| Tests | `test_enroll_validates_agent_supplied_fields`, `test_patch_config_bumps_version_validates_and_audits`. Needed in C: unknown metric name rejected (not stored), label value length cap and control-character rejection, NaN/Inf dropped, > `max_batch_metrics` handled, unknown `schema` → 400, oversize → 413, a name like `x'; DROP TABLE metrics;--` never reaches SQL text. |
| Gaps | (a) Label **values** (mount path, iface name) are agent-controlled free text: C must cap length and character set, and must not let them create unbounded series (suggest max 32 distinct mounts and 16 interfaces per host). (b) Existing disk metrics are stored as `disk_used_percent__<mount>` (`app/collector/polling_model.py`); C must map the contract's `disk_used_percent{mount}` onto a name that respects `metric_history.metric_name VARCHAR(150)`. |

### 4. Enrollment-key brute force and abuse

| | |
|---|---|
| Impact | Unauthorized host enrolled into an account; with a leaked valid key an attacker can also re-enroll an existing hostname, which rotates its token and silences the real agent. |
| Controls | 192-bit random key (guessing is infeasible); format check before any DB access; one conditional `UPDATE` decides validity (single-use keys cannot be spent twice under contention); expiry enforced by the DB clock; one indistinguishable `401` message for every failure; per-IP limit in the app (Redis, `HOST_AGENT_ENROLL_RATE_PER_HOUR`) **and** per-IP limit in nginx (`ha_enroll`, burst 10) that still works if Redis is down (the app limiter fails open); real client IP comes from uvicorn `--proxy-headers` (rightmost untrusted value, so a client-supplied `X-Forwarded-For` cannot choose its bucket); enrollment is audited. |
| Where | `app/api/host_agents.py` `enroll`; `app/auth/rate_limit.py` `check_rate_limit`; `deploy/nginx.conf` `location = /api/host-agents/enroll`. |
| Tests | `test_enroll_is_rate_limited_before_the_key_is_checked`, `test_enroll_rejects_bad_keys_with_one_indistinguishable_error`, `test_single_use_key_works_once`, `test_enroll_validity_is_one_conditional_update_not_read_then_write`, real DB: `test_single_use_key_is_spent_exactly_once_under_contention`, `test_multi_use_key_never_exceeds_max_uses_under_contention`, `test_expiry_is_enforced_by_the_database_clock`; nginx: `tests/test_host_agent_nginx_conf.py`. |
| Gaps | (a) **Revocation is reversible by any valid key**: enroll's upsert sets `status = 'active'` on a revoked host, so a revoked host (or an attacker holding a valid key and the hostname) can come back. Operational rule: revoke the key as well, keep keys short-lived and `max_uses` small. Decision for the owner: refuse re-enrollment of a `revoked` host until an admin clears it. (b) A key leaked **before** use can hijack an existing hostname in its account (see Impact). (c) Keys are not bound to a hostname or source network. |

### 5. Oversized or slow-loris ingest

| | |
|---|---|
| Impact | Worker exhaustion: uvicorn runs 2 workers on a shared 2-vCPU box. |
| Controls | nginx `client_max_body_size 256k` on ingest (16k on enroll), `client_body_timeout 10s`, `proxy_request_buffering on` (nginx reads the whole body, so a slow sender never holds a uvicorn worker), `limit_req` + `limit_conn` per IP, upstream timeouts, keepalive upstream. App cap `HOST_AGENT_MAX_BODY_BYTES` → 413 (must match nginx). |
| Where | `deploy/nginx.conf`; app-side cap = `CONTRACT` (phase C). |
| Tests | `tests/test_host_agent_nginx_conf.py` (directives, limits equal to defaults, `nginx -t` when nginx is installed). Verified by hand against a stub backend: 300 000-byte body → 413, 262 144 bytes → proxied, burst on enroll → 429 after the burst. Needed in C: chunked body over the limit → 413 without reading it all; the app cap holds when the app is reached without nginx (dev). |
| Gaps | (a) `X-Forwarded-For`-independent: limits key on the TCP peer, so many hosts behind one NAT share a bucket (`ha_ingest` is 600 r/m, burst 60; raise it if more than ~600 hosts at 60 s (or ~150 at 15 s) share one egress IP). (b) Header slow-loris is handled by nginx defaults (`client_header_timeout` 60s at server level; not tightened here to avoid touching existing behaviour). (c) The app limiter is Redis-based and fails open. |

### 6. Clock skew

| | |
|---|---|
| Impact | Samples dropped (agent clock ahead/behind), or a host with a wrong clock writes history at wrong times. |
| Controls | Server validates each sample `ts` (not `sent_at`) against hub time: > 10 min future or > 24 h past is dropped and reported in `rejected`. Hub uses chrony (`deploy/deploy.sh` installs it). Agent `sent_at` is advisory. |
| Where | `CONTRACT` (phase C). |
| Tests | Needed in C: boundaries at +10 min / −24 h, `rejected` count, host still marked seen. |
| Gaps | A host with a clock 5 min ahead is accepted but its charts are shifted; the hub does not alert on skew. Troubleshooting in `docs/HOST_AGENT.md`. |

### 7. Cross-account leakage

| | |
|---|---|
| Impact | One account's operators see or modify another account's hosts or data. |
| Controls | Every admin read/write is scoped with `get_accessible_account_ids` and answers 404 (not 403) for out-of-scope ids; ingest takes the account from the token, never from the payload; `UNIQUE (aws_account_id, hostname)` keeps equal hostnames in two accounts separate; enrollment keys are bound to one account and the key's account must still exist; no FK to users/accounts so the audit trail survives deletes. |
| Where | `app/api/host_agents.py` (`_scoped_clause`, `_in_scope` in list, keys, revoke, config); `authenticate_agent`. |
| Tests | `test_scope_leakage_user_of_account_1_cannot_see_or_touch_account_2`, `test_empty_scope_sees_nothing`, `test_same_hostname_in_two_accounts_is_two_hosts`, `test_key_for_missing_account_is_404`, `test_permissions`, `test_rbac_matrix_lists_every_host_agent_route_with_the_right_guard`, real DB `test_full_flow_on_real_sql_scope_state_revoke_config`. Needed in C: a payload claiming another host's name/instance_id is stored under the token's host only. |
| Gaps | The ingested samples map to hub resources through `resource_id` (phase C/D). The mapping from a host to an existing `resources` row (by `instance_id`) must check the resource's account equals the token's account; otherwise a token holder could write into another account's resource history. **Highest-priority C review item.** |

### 8. Other items reviewed

| Threat | Result |
|---|---|
| Feature accidentally on | Default `false`; disabled = 404 on all routes before auth (`test_every_route_is_404_when_disabled_even_unauthenticated_and_touches_nothing`, `test_flag_defaults_to_false`). |
| Secrets in logs/audit | Tokens and keys never logged or audited (`test_enroll_returns_token_once_and_stores_only_hashes`; the enroll log line carries only the host id). nginx access logs do not contain request bodies; the `Authorization` header is not in the default log format. |
| Credential table growth | `HOST_AGENT_CREDENTIAL_RETENTION_DAYS` pruning (`tests/test_host_agent_retention.py`). |
| Binary/installer tampering | Distribution design with offline signing key: `docs/HOST_AGENT_DISTRIBUTION.md`; `scripts/host_agent_release.sh` (`tests/test_host_agent_release.py`). Hub compromise remains fleet compromise unless agents verify the offline-key signature (CONTRACT for phase E). |
| Install-time exposure | The install command line carries the enrollment key: short expiry, `max_uses` = batch size, revoke after use. |
| `Connection: upgrade` on `/api/` | Pre-existing: the generic `/api/` location sends `Connection: upgrade` on every request, which defeats upstream keepalive there. The agent locations use their own upstream with `Connection ""`. Left unchanged. |

## Priority list

1. (C) Resource mapping must enforce account equality (threat 7).
2. (C) Metric/label allowlists and caps, finite-value checks, ts window, `seq` rules (threats 2, 3, 6) with the tests listed above.
3. (B, owner decision) Re-enroll of a revoked host (threat 4a).
4. (C) Dedup or accept duplicate rows (threat 2b).
5. Token expiry/rotation (threat 1a): not in the contract; a follow-up.
