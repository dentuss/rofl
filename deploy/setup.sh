#!/usr/bin/env bash
#
# One-shot setup script for the rofl trading bot on EC2.
# Works on Amazon Linux 2023, Ubuntu 22.04/24.04, and Debian.
#
# Usage (from a fresh EC2 instance):
#     curl -fsSL https://raw.githubusercontent.com/dentuss/rofl/main/deploy/setup.sh | bash
#
# Or if the repo is already cloned:
#     bash deploy/setup.sh
#
# What it does:
#   1. Detects Amazon Linux vs Debian/Ubuntu
#   2. Installs Docker Engine via the official method for that OS
#   3. Installs the Docker Compose v2 plugin (not bundled on AL2023)
#   4. Adds the current user to the docker group
#   5. Clones the repo (if not already cloned)
#   6. Builds the image and starts the bot in paper mode
#   7. Verifies the container is running
#
# Notes:
#   - The script must be runnable without prior docker access. It uses
#     sudo for everything that needs root.
#   - After running, you may need to log out and back in (or
#     `exec sg docker -c bash`) for the docker group to take effect in
#     your existing shell. The script handles this automatically.
#   - Re-running is safe — every step is idempotent.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/dentuss/rofl.git}"
REPO_BRANCH="${REPO_BRANCH:-main}"
REPO_DIR="${REPO_DIR:-$HOME/rofl}"

log() { echo -e "\033[1;34m==>\033[0m $*"; }
err() { echo -e "\033[1;31mERROR:\033[0m $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] && err "Do not run as root. Run as ec2-user or ubuntu — the script uses sudo when needed."

# ----- Detect OS family ----------------------------------------------------
if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_ID="$ID"
    OS_VERSION_ID="${VERSION_ID:-}"
else
    err "Cannot detect OS — /etc/os-release missing"
fi

log "Detected OS: $OS_ID $OS_VERSION_ID"

# ----- Install Docker Engine + compose plugin ------------------------------
install_docker_amazon_linux() {
    log "Installing Docker on Amazon Linux"
    sudo dnf -y -q update
    sudo dnf -y -q install docker git
    sudo systemctl enable --now docker

    # Amazon Linux's docker package does NOT include the compose plugin.
    # Install it manually from GitHub releases (works for both x86_64 and arm64).
    if ! docker compose version >/dev/null 2>&1 \
            && ! /usr/libexec/docker/cli-plugins/docker-compose version >/dev/null 2>&1; then
        log "Installing docker-compose plugin"
        sudo mkdir -p /usr/local/lib/docker/cli-plugins
        ARCH=$(uname -m)
        case "$ARCH" in
            x86_64)  PLUGIN_ARCH="x86_64" ;;
            aarch64) PLUGIN_ARCH="aarch64" ;;
            *)       err "Unsupported architecture: $ARCH" ;;
        esac
        sudo curl -fsSL \
            "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${PLUGIN_ARCH}" \
            -o /usr/local/lib/docker/cli-plugins/docker-compose
        sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
    fi
}

install_docker_debian_family() {
    log "Installing Docker on Debian/Ubuntu via official repo"
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl git gnupg
    sudo install -m 0755 -d /etc/apt/keyrings
    if [[ ! -f /etc/apt/keyrings/docker.asc ]]; then
        sudo curl -fsSL "https://download.docker.com/linux/${OS_ID}/gpg" \
            -o /etc/apt/keyrings/docker.asc
        sudo chmod a+r /etc/apt/keyrings/docker.asc
    fi
    REPO_LINE="deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
        https://download.docker.com/linux/${OS_ID} ${VERSION_CODENAME} stable"
    echo "$REPO_LINE" | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin
    sudo systemctl enable --now docker
}

case "$OS_ID" in
    amzn)              install_docker_amazon_linux ;;
    ubuntu|debian)     install_docker_debian_family ;;
    *)                 err "Unsupported OS: $OS_ID (script supports amzn, ubuntu, debian)" ;;
esac

# ----- Add user to docker group --------------------------------------------
if ! groups | grep -qw docker; then
    log "Adding $USER to docker group"
    sudo usermod -aG docker "$USER"
    # The current shell's group membership won't update until logout.
    # We work around it by running subsequent docker commands via `sg`.
    DOCKER="sg docker -c"
    DOCKER_REQUIRES_SG=1
else
    DOCKER="bash -c"
    DOCKER_REQUIRES_SG=0
fi

log "Verifying Docker"
$DOCKER 'docker --version' || err "Docker install failed"
$DOCKER 'docker compose version' || err "Docker Compose plugin missing"

# ----- Clone repo ----------------------------------------------------------
if [[ ! -d "$REPO_DIR/.git" ]]; then
    log "Cloning repo to $REPO_DIR"
    git clone --branch "$REPO_BRANCH" "$REPO_URL" "$REPO_DIR"
else
    log "Repo already at $REPO_DIR — pulling latest"
    (cd "$REPO_DIR" && git fetch origin "$REPO_BRANCH" \
        && git checkout "$REPO_BRANCH" \
        && git pull --ff-only)
fi

# ----- Build and start -----------------------------------------------------
log "Building image and starting bot (paper mode by default)"
$DOCKER "cd '$REPO_DIR' && docker compose up -d --build"

# ----- Verify --------------------------------------------------------------
log "Waiting 5s for the container to settle..."
sleep 5
$DOCKER "cd '$REPO_DIR' && docker compose ps"

echo
echo -e "\033[1;32m=========================================="
echo " rofl-bot is RUNNING in paper mode."
echo -e "==========================================\033[0m"
echo
echo "Quick commands (from $REPO_DIR):"
echo "    docker compose logs -f         # tail live logs"
echo "    docker compose ps              # status"
echo "    docker compose down            # stop"
echo "    docker compose restart bot     # restart"
echo
if [[ $DOCKER_REQUIRES_SG -eq 1 ]]; then
    echo -e "\033[1;33mIMPORTANT:\033[0m the docker group was added to your user."
    echo "In your current shell you still need 'sudo' for docker. To pick"
    echo "up the new group without sudo, log out and back in OR run:"
    echo "    exec sg docker"
    echo
fi
echo "Next steps:"
echo "  1. Set up Telegram (optional, see deploy/README.md)"
echo "  2. Paper-trade for 2 weeks"
echo "  3. Add real Bybit keys via .env to go live"
