# Self-hosting the paper program on a laptop

What runs at home vs what never does:

| runs on the laptop | stays on EC2 |
|---|---|
| `docker-compose.bidir4h-paper.yml` — 16 paper legs + tg-control, **no keys** | `docker-compose.bidir4h-live.yml` — REAL MONEY |
| `docker-compose.collector.yml` — tick collector, public websockets, **no keys** | |
| daily `sleeves_paper.py` + `xs_paper.py` (cron, read-only public data) | |

**Never put the trading keys on the laptop.** They are IP-whitelisted to the
EC2 Elastic IP; a home IP is dynamic and a roaming machine is a bigger
attack surface. Everything below is deliberately keyless (the only secret
is the optional Telegram token).

## 1. OS choice

| option | verdict |
|---|---|
| **Ubuntu Server 24.04 LTS (minimal)** | **Recommended.** Headless, 5-year LTS, `deploy/setup.sh` supports it natively, best docs when something breaks. ~300 MB RAM idle. |
| Debian 12 | Equally good, slightly leaner; pick it if you already know it. setup.sh supports it. |
| Ubuntu Desktop | Only if the machine stays a dual-use laptop. The GUI wastes ~1–1.5 GB RAM and adds update prompts; sleep management fights you. |
| Windows + WSL2/Docker Desktop | Works for a demo; fragile for 24/7 (host sleep, Docker Desktop updates, WSL clock drift after sleep). Not for the trial. |

Dedicated old laptop → **Ubuntu Server 24.04 LTS**. During installation:
tick **Install OpenSSH server**, use guided full-disk, skip snaps.

## 2. Hardware bar

- **RAM ≥ 8 GB** (17 paper containers ≈ 3.5–4.5 GB + collector ~0.2 GB +
  OS; 8 GB runs without swap pressure — add a 2 GB swapfile anyway,
  see deploy/README.md for the snippet).
- Any 2-core x86_64 from the last decade. CPU is idle 99% of the time
  (bots think for seconds once per 4h; the collector is I/O-bound).
- **Disk ≥ 60 GB**: collector ≈ 5–10 MB/day gzipped, docker logs capped by
  the composes, bar caches a few GB — years of headroom.
- **Ethernet > Wi-Fi.** If Wi-Fi is unavoidable:
  `iw dev wlan0 set power_save off` (and make it persistent via a udev rule
  or netplan `wifi.powersave: false`) — Wi-Fi powersave causes websocket
  drops the collector will log as reconnects.

## 3. Laptop-as-server config (the part everyone forgets)

```bash
# a) closing the lid must not suspend
sudo sed -i 's/#\?HandleLidSwitch=.*/HandleLidSwitch=ignore/;
             s/#\?HandleLidSwitchExternalPower=.*/HandleLidSwitchExternalPower=ignore/' \
    /etc/systemd/logind.conf
sudo systemctl restart systemd-logind

# b) no sleep, ever
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target

# c) clock MUST be NTP-synced (timestamps + any future authed use)
timedatectl                     # want: "System clock synchronized: yes"
sudo timedatectl set-timezone UTC

# d) battery is your built-in UPS — but don't cook it at 100% forever.
# If supported, cap charge at ~80%:
echo 80 | sudo tee /sys/class/power_supply/BAT0/charge_control_end_threshold \
    2>/dev/null || echo "no threshold support — consider TLP"

# e) security patches without babysitting
sudo apt-get install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Also: in BIOS/UEFI enable **"Restore on AC power"** (auto-boot after an
outage) if the machine has it; keep the laptop on a hard surface, dust the
fans once.

## 4. Deploy

```bash
# docker + compose + repo clone + image prebuild (starts NOTHING):
bash <(curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh)
# log out/in for the docker group, then:
cd ~/rofl

# optional but recommended — Telegram for the button panel + bot pushes:
cat > .env <<'EOF'
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
EOF
chmod 600 .env

# the paper stack (17 containers) + the collector (1):
docker compose -f docker-compose.bidir4h-paper.yml up -d --build
docker compose -f docker-compose.collector.yml up -d --build
sudo systemctl enable docker        # stacks auto-resume on boot
```

Verify: `docker ps` → 18 containers, paper bots turning `healthy` within
~3 min; Telegram panel answers with Stats (booked-only — correct, no keys);
`docker exec rofl-collector ls /app/data/$(date -u +%F)` shows csv files
growing.

## 5. Daily sleeve trackers (cron)

```bash
sudo apt-get install -y python3-venv
cd ~/rofl && python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# FIRST RUN ONLY — preserve the anchor dates already stamped during
# research (state/ does not travel through git; without these envs the
# forward record would restart from today):
SLEEVES_ANCHOR=2026-07-06 ./.venv/bin/python sleeves_paper.py
XS_ANCHOR=2026-07-09     ./.venv/bin/python xs_paper.py

crontab -e    # add (00:10 UTC, after the daily close):
# 10 0 * * * cd $HOME/rofl && ./.venv/bin/python sleeves_paper.py >> state/sleeves_cron.log 2>&1
# 12 0 * * * cd $HOME/rofl && ./.venv/bin/python xs_paper.py     >> state/xs_cron.log 2>&1
```

Missed days are harmless — the signals are lagged and deterministic, so the
next run recomputes the identical history; the anchor is what makes it a
forward record.

## 6. Remote access (optional, recommended)

Tailscale beats port forwarding — zero config, no exposed ports:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up      # then ssh laptop from anywhere via its tailscale name
```

Keep SSH key-only: in `/etc/ssh/sshd_config.d/hardening.conf` set
`PasswordAuthentication no`, then `sudo systemctl reload ssh`.

## 7. What normal looks like / weekly care

- `-p` (pullback) legs idle for weeks: **correct** (~1 trade/6wk/name).
- Collector logging occasional `retrying in 5s`: normal reconnects.
- Weekly: press Telegram **Health** (all heartbeats green), glance at
  `df -h` and `docker stats` once; skim `state/*_cron.log` tails.
- Reboots/outages: everything is `restart: unless-stopped` + docker
  enabled on boot — the stack self-resumes; the collector just has a gap.
- If you care about the tick archive long-term, rsync it off occasionally:
  `docker run --rm -v rofl-collector_tick_data:/d -v $PWD:/out alpine tar czf /out/ticks-$(date +%F).tgz /d`

## 8. Migration notes

Paper state is disposable by design (it's paper) — moving machines means
`down -v` and fresh anchors EXCEPT the sleeve trackers, whose anchors you
carry via the env vars in §5. The collector's data volume is the only thing
worth migrating (§7 rsync). The live stack on EC2 is untouched by any of
this and must never share a machine with experiments.
