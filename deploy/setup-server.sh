#!/usr/bin/env bash
# One-time server preparation for a fresh Ubuntu 22.04/24.04 VM
# (Oracle Cloud Always Free A1 arm64, DigitalOcean, Hetzner, …).
#
#   ssh ubuntu@<VM_IP> 'bash -s' < deploy/setup-server.sh
#
# What it does:
#   1. installs Docker Engine + the compose plugin
#   2. opens TCP 80/443 in the OS firewall (Oracle images ship iptables
#      rules that DROP everything except SSH — the console security list
#      alone is not enough)
#   3. creates /opt/nexus for the app
# It does NOT deploy the app — see deploy/push-from-laptop.sh.
set -euo pipefail

if [[ $(id -u) -ne 0 ]]; then SUDO=sudo; else SUDO=; fi

echo "==> Packages"
$SUDO apt-get update -y
$SUDO apt-get install -y --no-install-recommends ca-certificates curl gnupg rsync git

if ! command -v docker >/dev/null 2>&1; then
  echo "==> Docker"
  curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO usermod -aG docker "$USER" || true
$SUDO systemctl enable --now docker

echo "==> Firewall: allow 80/443"
if command -v iptables >/dev/null 2>&1; then
  for p in 80 443; do
    $SUDO iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null \
      || $SUDO iptables -I INPUT 6 -p tcp --dport "$p" -m state --state NEW -j ACCEPT
  done
  # HTTP/3
  $SUDO iptables -C INPUT -p udp --dport 443 -j ACCEPT 2>/dev/null \
    || $SUDO iptables -I INPUT 6 -p udp --dport 443 -j ACCEPT
  if command -v netfilter-persistent >/dev/null 2>&1; then
    $SUDO netfilter-persistent save || true
  else
    $SUDO apt-get install -y --no-install-recommends iptables-persistent >/dev/null 2>&1 || true
    $SUDO netfilter-persistent save 2>/dev/null || true
  fi
fi
if command -v ufw >/dev/null 2>&1 && $SUDO ufw status | grep -q "Status: active"; then
  $SUDO ufw allow 80/tcp && $SUDO ufw allow 443/tcp && $SUDO ufw allow 443/udp
fi

echo "==> App directory"
$SUDO mkdir -p /opt/nexus
$SUDO chown -R "$USER":"$USER" /opt/nexus

# Light hardening that costs nothing: unattended security updates.
$SUDO apt-get install -y --no-install-recommends unattended-upgrades >/dev/null 2>&1 || true

echo
echo "Server ready. Log out and back in once so the docker group applies, then run"
echo "  deploy/push-from-laptop.sh <VM_IP>   from your laptop."
docker --version || true
