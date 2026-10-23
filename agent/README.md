# cloudops-agent (host agent)

Small static Go binary that runs on a server, reads `/proc` and `/sys`, and **pushes** metrics to the
CloudOps hub (`/api/host-agents/*`). It is **not** the AI agent (`app/agent`, `/api/agent`). It opens no
listening port and runs as the unprivileged `cloudops-agent` user.

```
cloudops-agent enroll --url https://<hub> --key cak_...   # writes /etc/cloudops-agent/agent.json (0600)
cloudops-agent run                                        # what systemd runs
cloudops-agent check                                      # prints the payload it would send; no network
cloudops-agent version
```
Exit code 3 = hub answered 401 (revoked); the unit does not restart on it.

Build / test: `make test` · `make dist` (linux amd64+arm64, `CGO_ENABLED=0`, writes `dist/SHA256SUMS`).
Install: `sudo ./install.sh --base-url https://<host serving dist/> --url https://<hub> --key cak_...`
(or `--binary dist/cloudops-agent-linux-amd64 --sums dist/SHA256SUMS`). Always sha256-verified.
Manual DEV end-to-end test: `scripts/e2e-dev.sh`.

Files: state `/var/lib/cloudops-agent/state.json` (seq counter + last hub config), offline queue
`/var/lib/cloudops-agent/queue/` (default 20 MB, oldest dropped first, entries >23 h old not replayed).
`agent.json` may set `"no_gzip": true` for a hub that cannot decode gzip request bodies.
