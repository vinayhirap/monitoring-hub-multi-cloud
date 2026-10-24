# Host agent distribution, signing and auto-update

What exists in this repo: nginx download location (`deploy/nginx.conf`, `/dl/host-agent/`), the staging/signing/verification script (`scripts/host_agent_release.sh`) and its tests. What is **design only**: the agent's update loop and the agent-side signature check. `install.sh`, `install-macos.sh` and `install.ps1` live in `agent/` and verify the binary against `SHA256SUMS`; `stage` only publishes `install.sh` if it is copied into the binaries directory. Items are tagged **[IN REPO]** or **[DESIGN]**.

## 1. Layout served by nginx **[IN REPO]**

```
https://<HUB_HOST>/dl/host-agent/manifest.json          signed pointer to the latest version
https://<HUB_HOST>/dl/host-agent/manifest.json.sig
https://<HUB_HOST>/dl/host-agent/<version>/cloudops-agent-<os>-<arch>
https://<HUB_HOST>/dl/host-agent/<version>/install.sh
https://<HUB_HOST>/dl/host-agent/<version>/SHA256SUMS
https://<HUB_HOST>/dl/host-agent/<version>/SHA256SUMS.sig
```

On the hub the directory is `/opt/monitoring-hub/host-agent-dist/` (mapped by an `alias`; read-only `GET`/`HEAD`, no listing, 60 req/min/IP). It is outside the git checkout so `git pull` and deploys never touch it. Versions are **immutable**: the staging script refuses to overwrite an existing version; fix a bad release by publishing a higher version and moving `manifest.json` forward.

Everything here is public on purpose and must contain **no secrets** (no enrollment keys inside `install.sh`). Integrity comes from the signature, transport security from TLS (agents refuse `http://`).

## 2. Releasing **[IN REPO]**

On a build/admin machine (not the hub):

```
scripts/host_agent_release.sh stage  1.2.0 ./build ~/keys/release.key.pem ./host-agent-dist
scripts/host_agent_release.sh verify 1.2.0 agent/keys/release.pub.pem ./host-agent-dist
rsync -a ./host-agent-dist/ hcsadmin@<DEV_HOST>:/opt/monitoring-hub/host-agent-dist/      # DEV first
# after the soak: same copy to PROD
```

`stage` copies `cloudops-agent-<os>-<arch>` files and `install.sh`, writes `SHA256SUMS`, signs it, then writes and signs `manifest.json`. `HOST_AGENT_MIN_VERSION=1.1.0` sets `min_version` (agents older than this must update at once; used to retire a bad release). Copy the version directory **before** `manifest.json`, so a manifest never points at files that are not there yet.

## 3. Signing key handling

- Algorithm: Ed25519, raw signature over the exact bytes of `SHA256SUMS` / `manifest.json` (OpenSSL 3 `pkeyutl -rawin` to sign; Go stdlib `crypto/ed25519` to verify, so the agent needs no dependencies).
- Generate **once, offline**: `openssl genpkey -algorithm ed25519 -out release.key.pem`, then `openssl pkey -in release.key.pem -pubout -out release.pub.pem`.
- The private key never lives on the hub, DEV, PROD, a CI runner with broad access, or in git. Keep it encrypted (passphrase manager or hardware token) on an admin machine, with one offline backup stored separately. Two people know where it is.
- The public key is committed (`agent/keys/release.pub.pem`) and **compiled into the agent**; install.sh ships the same key fingerprint. A copy of the key served by the hub proves nothing, so the agent never trusts a key it downloads.
- Rotation: the agent accepts a list of public keys. Publish a release signed by the old key that embeds the new key, then switch signing. Compromise: build an agent version with the old key removed, set `min_version` to it, sign with the new key, and tell hosts to reinstall (an attacker holding the old key can still serve old, validly signed versions, which `min_version` plus the version check below refuse).

## 4. First install trust

install.sh is fetched over TLS and then verifies the binary against `SHA256SUMS`, and `SHA256SUMS` against its signature with `openssl pkeyutl -verify -rawin` when OpenSSL 3 is present. Honest limits:

- The first download is trusted via TLS to the hub plus the operator checking `install.sh`'s SHA-256 out of band (publish the expected hash in the PROD runbook ticket, not on the hub).
- Hosts with OpenSSL < 3 cannot verify the signature in shell. For them install.sh stops unless run with an explicit `--no-verify-signature`, and the checksum is still checked; the agent re-verifies the signature on the first update.
- If the hub is compromised and the signing key is not, existing agents are protected on update (they verify) but new installs are only as safe as the first-download check. State this in the PROD approval.

## 5. Auto-update **[DESIGN]** (not implemented: it needs the agent from phase E)

Goal: fleet updates without SSH, safe against a bad release and a compromised hub.

Agent loop (every 6 h ± 30 min jitter, or when an ingest response signals `update_available`, an optional field phase C may add; polling alone is enough):
1. `GET manifest.json` and `manifest.json.sig`; verify with the embedded keys. Reject if `latest` ≤ the running version (no downgrade), or if the manifest is older than the last accepted one (store the last seen `released_at`).
2. If `latest` > running (or running < `min_version`): download `<latest>/SHA256SUMS` + `.sig`, verify, take the line for this `<os>-<arch>`, download the binary to a temp file in the install directory (same filesystem), verify SHA-256, `chmod`, run `<new> --self-test` (loads config, resolves the hub name, no push).
3. **Atomic swap**: `rename(new, cloudops-agent)` after copying the old binary to `cloudops-agent.prev`; then exit and let systemd (`Restart=always`) start the new binary. Write `update.state` = `{from, to, started_at, boots: 0}`.
4. **Rollback on crash loop**: on every start the agent increments `boots` while `update.state` is `pending`. It marks the update `healthy` after one successful ingest (HTTP 200). If systemd restarts it ≥ 3 times within 10 minutes without reaching `healthy`, an `ExecStartPre` guard (`cloudops-agent --rollback-if-unhealthy`, a few lines, no network) restores `.prev` and records the failed version; the agent will not retry that version.
5. Staged rollout without server code: the manifest can carry `{"rollout_percent": 10}` and each agent hashes `host_agent_id` into a bucket; it updates only if its bucket < percent. Ramp 10 → 50 → 100 by re-signing the manifest.

Kill switch: set `latest` to the previous version and `min_version` unchanged; agents never downgrade by themselves, so a bad release is stopped by publishing a fixed higher version, or by the rollback guard above.

Not decided here: Windows (service model, file locking during swap), and whether `HOST_AGENT_ENABLED=false` should also freeze updates (suggest yes: an agent that gets 404 on ingest skips the update check).

## 6. Checks before publishing a release

1. `scripts/host_agent_release.sh verify` passes on the staged directory.
2. DEV hosts updated from this exact directory and healthy for 24 h.
3. `curl -sI https://<HUB_HOST>/dl/host-agent/<version>/SHA256SUMS` returns 200 with the security headers present (single copy).
4. The expected `install.sh` SHA-256 is recorded in the rollout ticket.
