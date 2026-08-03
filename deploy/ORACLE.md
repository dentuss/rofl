# Hosting on Oracle Cloud Always-Free (ARM A1) — the runbook

**Verdict: feasible and a good call.** The EC2 box was `t4g` = ARM Graviton,
and Oracle's A1 is also ARM (aarch64) — so the image, wheels (pandas/numpy/
sklearn/ccxt all ship aarch64), and `deploy/setup.sh` (already handles
`aarch64`) run **unchanged**. 24 GB RAM is ~5× what the whole stack needs, so
one free A1 instance can host **everything at once**: the live book, the
paper program, and the collector. At L1 unit-weight scale the book's expected
profit is ~$9–19/mo, so replacing a ~$30/mo EC2 with a $0 box is not a
rounding error — it's the difference between the edge paying for itself or not.

## 0. The two caveats that actually matter (read before creating anything)

1. **Idle-reclamation.** Oracle may reclaim Always-Free compute it deems idle
   (low CPU/RAM/network over ~7 days). Our bots are CPU-idle 99% of the time,
   so a **live-money box could get reclaimed under a position.** The fix is
   not a keep-busy hack — it's to **upgrade the tenancy to Pay-As-You-Go
   (PAYG)**. PAYG exempts you from idle-reclamation and keeps Always-Free
   resources genuinely $0 as long as you stay within the free limits (4
   OCPU / 24 GB / 200 GB block / 10 TB egﬀress). Add a card, get exempted,
   pay nothing. **Do this before running live.**
2. **Capacity ("Out of host capacity").** A1 Always-Free is popular and
   often shows this error at create time. It is transient. Fixes, in order:
   pick a less-busy home region at signup (avoid the biggest ones), try each
   Availability Domain (AD-1/2/3), retry at off-peak hours, or run a
   create-retry loop (community scripts like `oci-arm-host-capacity` exist;
   or just re-click every few hours). Once created, it's yours.

Even with both handled, the design already degrades safely if the box dies:
positions carry **exchange-side SL/TP** (fire autonomously, bot down or not),
closes are **reduce-only**, and resume **reconciles** state vs exchange and
**HALTs** on anything it didn't open. Box loss = a restart-or-reconcile
event, never an unprotected position.

## 1. Create the instance

1. Sign up at cloud.oracle.com. Pick your home region thoughtfully (see
   capacity note). Complete card verification.
2. **Upgrade to Pay-As-You-Go** (Governance → **Upgrade to Paid**) — this is
   the anti-reclamation step. You stay on free resources.
3. Compute → **Create Instance**:
   - Image: **Canonical Ubuntu 24.04** (Minimal is fine).
   - Shape: **VM.Standard.A1.Flex**, **4 OCPUs / 24 GB** (the full free
     allotment in one VM).
   - Boot volume: **100 GB** (free block budget is 200 GB total; years of
     headroom for the collector).
   - SSH: paste your **public key** (`~/.ssh/id_ed25519.pub`).
   - Networking: default VCN is fine.
4. When it's running, note the **public IP**.

## 2. Reserve a static IP + open only SSH

The Bybit keys are IP-whitelisted, so the box needs a **stable** IP.

1. Networking → **Reserved Public IPs** → reserve one (free), then attach it
   to the instance's VNIC (edit the primary private IP → **Reserved public
   IP**). The IP is now permanent across stop/start.
2. Security List (or an NSG) on the VCN subnet:
   - **Ingress**: TCP 22 from **your** IP only. Nothing else — the bots,
     collector, and Telegram are all **outbound**; no inbound is needed.
   - **Egress**: allow all (default).

> Ubuntu's Oracle image also ships host `iptables` that drop inbound except
> SSH — which is exactly what we want, so leave it. If containers ever can't
> reach the internet after installing Docker (rare), it's the host FORWARD
> chain fighting Docker's; fix with `sudo iptables -P FORWARD ACCEPT &&
> sudo netfilter-persistent save` and `sudo systemctl restart docker`.

## 3. Box prep

SSH in as `ubuntu`, then:

```bash
sudo timedatectl set-timezone UTC
timedatectl                    # want "System clock synchronized: yes" (Bybit auth breaks on skew)
bash <(curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh)
# log out/in (docker group), then: cd ~/rofl
```

`setup.sh` installs Docker + Compose (auto-detects aarch64), clones the repo,
and pre-builds the image — **it starts nothing.**

24 GB means no swap is needed, but a 2 GB file is cheap insurance:
```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile
sudo swapon /swapfile && echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

## 4. Re-whitelist the keys (both accounts)

On Bybit, edit **both** API keys (main = triple book, sub = pullback book)
and change the IP allowlist to the **new reserved Oracle IP**. Confirm with
the preflight from `deploy/LIVE.md` §4 (both must return their ~$900.00
balances from the new box).

## 5. Bring it up

One box, three isolated stacks (distinct project names / image tags /
volumes — they cannot collide):

```bash
cd ~/rofl && git pull
# .env: both key pairs + Telegram (see deploy/LIVE.md §3 for the template)

# LIVE (real money) — the go-live gates in ROADMAP Phase 6 still apply:
docker compose -f docker-compose.bidir4h-live.yml up -d --build

# PAPER + COLLECTOR (keyless, safe to co-locate):
docker compose -f docker-compose.bidir4h-paper.yml up -d --build
docker compose -f docker-compose.collector.yml   up -d --build

sudo systemctl enable docker    # auto-resume on reboot
```

`docker ps` → ~35 containers (16 live + 16 paper + 2 tg + 1 collector), RAM
~8–9 GB of 24. If you'd rather keep live isolated, run only the live stack
here and the paper/collector on the laptop per `deploy/SELFHOST.md` — both
are correct; co-locating is simpler and the paper stack holds no keys.

## 6. Resuming after the week down (do this on first live start)

The bot was stopped for a week. On the first live `up`, each leg reconciles
against Bybit automatically:

- **Position still open on the exchange** → the bot resumes managing it
  (its attached SL/TP were live the whole time).
- **Position gone (SL/TP fired while down)** → `_book_resume_autonomous_close`
  books the real close from Bybit's closed-PnL history and arms/expires the
  cooldown from the actual fill time. Nothing is lost.
- **State says flat, exchange has something** → the startup sweep cancels
  stray orders and **HALTs** that leg for manual review (safe by design).

If you are **migrating live EC2 → Oracle** (not just restarting), do the
clean thing rather than copying volumes: on Bybit, confirm/flatten positions
manually if any remain; set each leg's `STARTING_EQUITY` in `.env` to the
**actual current balance** on that account; `up` fresh on Oracle (flat state
+ reconcile = clean start). Then `down` the EC2 stack and terminate the
instance. Never run both boxes live at once.

## 7. Steady state

- Telegram **Health** = all heartbeats green; **Reconcile** = booked≈real.
- `restart: unless-stopped` + `systemctl enable docker` → survives reboots
  and Oracle maintenance.
- Watch the first day's logs for `PENDING … maker limit` → `OPEN … (maker
  fill)`, the first TP-limit fill, and no `HALTED` lines.
- Kill criteria and the L1→L2→L3 progression are unchanged — see
  `research/ROADMAP.md` Phase 6.
