# EC2 deployment — SUPERSEDED

> ⚠ **The current host plan is Oracle Cloud A1 (free), not EC2 — see
> [`deploy/ORACLE.md`](ORACLE.md).** At L1 unit weights the book earns
> ~$9–17/mo, so a ~$30/mo EC2 consumes the entire edge; that is why the box
> moved. The instance sizing below (t4g.medium/large, 24 GiB) is retained for
> reference only. Current measured footprint is **124 MB shared + 198 MB
> private per leg** → 16 legs ≈ 3.29 GB (ORACLE.md §2), hosted on a 2 OCPU /
> 12 GB free allowance split across two VMs. Use
> `deploy/init-collector.sh` and `deploy/init-trading.sh` to build them.

**The complete go-live runbook is [`deploy/LIVE.md`](LIVE.md)** — accounts,
`.env`, preflight, start, week-1 checklist, kill criteria. This file only
covers the box itself. For running the KEYLESS parts (paper stack, tick
collector, sleeve trackers) on a home machine instead, see
[`deploy/SELFHOST.md`](SELFHOST.md).

> The pre-2026-07 version of this guide (single-bot presets, the 5-pair
> portfolio wrapper, +144%-CAGR claims) described the retired 1h program
> whose backtest edge was manufactured by an engine artifact — see
> `research/FINDINGS.md`. It lives in git history only.

## Instance

| | |
|---|---|
| Type | **t4g.medium + 4 GB swap** (economical) or **t4g.large** (comfortable) — see sizing note |
| AMI | Ubuntu 24.04 / 22.04 or Amazon Linux 2023 (arm64 or x86_64) |
| Disk | 24 GiB gp3 (includes the swapfile) |
| Region | `ap-southeast-1` (closest to Bybit) |
| Security group | inbound: SSH from your IP only; outbound: all (bot is outbound-HTTPS only) |
| Elastic IP | recommended — the Bybit keys are IP-whitelisted to it |

**Sizing note (17 containers):** each bot is a full Python process
(pandas + numpy + sklearn + ccxt ≈ 200–260 MB resident), so the stack wants
~3.5–4.4 GB — right at t4g.medium's 4 GB. CPU is irrelevant (bots think for
seconds once per 4h, staggered); memory is the constraint, and it is
import-time/idle-resident — exactly what swap absorbs well. On a medium,
add swap BEFORE first start:

```bash
sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

Watch `docker stats` and `free -h` in week 1; chronic swapping → resize to
t4g.large (2-minute stop/start; the Elastic IP and volumes survive, and
`restart: unless-stopped` brings the stack back on boot).

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
