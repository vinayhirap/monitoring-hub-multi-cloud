# cloudops-agent (host agent)

Small static Go binary for Linux, macOS and Windows (see `docs/HOST_AGENT_PLATFORMS.md` for per-OS metrics and
verification status) that reads the OS's own counters and **pushes** metrics to the
CloudOps hub (`/api/host-agents/*`). It is **not** the AI agent (`app/agent`, `/api/agent`). It opens no
listening port and runs as the unprivileged `cloudops-agent` user.

```
cloudops-agent enroll --url https://<hub> --key cak_...   # writes /etc/cloudops-agent/agent.json (0600)
cloudops-agent run                                        # what systemd runs
cloudops-agent check                                      # prints the payload it would send; no network
cloudops-agent version
```
Exit code 3 = hub answered 401 (revoked); the unit does not restart on it.

Build / test: `make test` · `make vet-all` · `make dist` (linux, darwin, windows x amd64+arm64, `CGO_ENABLED=0`, writes `dist/SHA256SUMS`).
Install (always sha256-verified), from the folder that holds `dist/` contents:
- Linux:   `sudo ./install.sh --binary ./cloudops-agent-linux-amd64 --sums ./SHA256SUMS --url https://<hub> --key cak_...` (or `--base-url https://<host serving dist/>`)
- macOS:   `sudo ./install-macos.sh --binary ./cloudops-agent-darwin-arm64 --sums ./SHA256SUMS --url https://<hub> --key cak_...`
- Windows (elevated PowerShell): `.\install.ps1 -Binary .\cloudops-agent-windows-amd64.exe -Sums .\SHA256SUMS -Url https://<hub> -Key cak_...`
Manual DEV end-to-end test: `scripts/e2e-dev.sh`.

Files: state `/var/lib/cloudops-agent/state.json` (seq counter + last hub config), offline queue
`/var/lib/cloudops-agent/queue/` (default 20 MB, oldest dropped first, entries >23 h old not replayed).
`agent.json` may set `"no_gzip": true` for a hub that cannot decode gzip request bodies.
