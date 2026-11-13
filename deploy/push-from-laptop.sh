#!/usr/bin/env bash
# Deploy (or redeploy) from your laptop to the VM without putting GitHub
# credentials on the server: rsync the repo, then build + start the stack.
#
#   deploy/push-from-laptop.sh <VM_IP> [ssh_user] [ssh_key]
#   e.g. deploy/push-from-laptop.sh 129.146.x.x ubuntu ~/.ssh/oracle.key
#
# First deploy: the server's /opt/nexus/.env must exist — the script copies
# .env.production.example there if it is missing and stops so you can fill
# it in (ssh in, `nano /opt/nexus/.env`), then run this script again.
set -euo pipefail

HOST=${1:?usage: $0 <VM_IP> [ssh_user] [ssh_key]}
USER_=${2:-ubuntu}
KEY=${3:-}
REMOTE=/opt/nexus
SSH=(ssh -o StrictHostKeyChecking=accept-new)
[[ -n "$KEY" ]] && SSH+=(-i "$KEY")
RSYNC_SSH="${SSH[*]}"

cd "$(dirname "$0")/.."

echo "==> Syncing code to $USER_@$HOST:$REMOTE"
rsync -az --delete \
  --exclude '.git' --exclude 'venv' --exclude '.venv' --exclude '__pycache__' \
  --exclude '.env' --exclude '.env.*.local' --exclude 'storage' --exclude '*.log' \
  --exclude 'scripts/eval_results' --exclude '.pytest_cache' \
  -e "$RSYNC_SSH" ./ "$USER_@$HOST:$REMOTE/"

echo "==> Ensuring .env exists"
if ! "${SSH[@]}" "$USER_@$HOST" "test -s $REMOTE/.env"; then
  "${SSH[@]}" "$USER_@$HOST" "cp $REMOTE/.env.production.example $REMOTE/.env"
  cat <<EOF

No .env on the server yet. I copied .env.production.example to $REMOTE/.env.
Fill it in now:

  ${SSH[*]} $USER_@$HOST
  nano $REMOTE/.env

then run this script again.
EOF
  exit 2
fi

echo "==> Building and starting (this takes a few minutes the first time: Chromium)"
"${SSH[@]}" "$USER_@$HOST" "cd $REMOTE && bash deploy/deploy.sh"
