# EC2 deployment

**The complete go-live runbook is [`deploy/LIVE.md`](LIVE.md)** — accounts,
`.env`, preflight, start, week-1 checklist, kill criteria. This file only
covers the box itself.

> The pre-2026-07 version of this guide (single-bot presets, the 5-pair
> portfolio wrapper, +144%-CAGR claims) described the retired 1h program
> whose backtest edge was manufactured by an engine artifact — see
> `research/FINDINGS.md`. It lives in git history only.

## Instance

| | |
|---|---|
| Type | **t4g.medium** or larger (17 containers; t4g.small OOMs) |
| AMI | Ubuntu 24.04 / 22.04 or Amazon Linux 2023 (arm64 or x86_64) |
| Disk | 20 GiB gp3 |
| Region | `ap-southeast-1` (closest to Bybit) |
| Security group | inbound: SSH from your IP only; outbound: all (bot is outbound-HTTPS only) |
| Elastic IP | recommended — the Bybit keys are IP-whitelisted to it |

## Setup

```bash
curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh | bash
```

> **Private repo?** `raw.githubusercontent.com` 404s anonymously. Clone with
> a PAT/SSH key first, then `bash deploy/setup.sh`.

The script installs Docker Engine + Compose v2, adds your user to the
`docker` group, clones/pulls the repo to `~/rofl`, and pre-builds the live
image. **It starts nothing** — the live-first program (ROADMAP Phase 6)
starts deliberately after the two Bybit accounts and `.env` are ready.
Re-running is safe; every step is idempotent.

Log out and back in (or `exec sg docker`) to use docker without sudo, then
continue with [`LIVE.md`](LIVE.md) step 2.

## Updating the box

```bash
cd ~/rofl && git pull
docker compose -f docker-compose.bidir4h-live.yml up -d --build   # recreates changed services only
```

State lives in named volumes (`rofl4h-live_live_*_state`) and survives
rebuilds, restarts, and instance stop/start (`restart: unless-stopped`
resumes the stack on boot). Never `down -v` a live stack unless you mean to
wipe the books — archive first.
