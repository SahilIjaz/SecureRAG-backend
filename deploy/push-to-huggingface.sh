#!/usr/bin/env bash
# Push the current branch to a Hugging Face Docker Space, which builds the
# Dockerfile and serves the API at https://<user>-<space>.hf.space
#
#   deploy/push-to-huggingface.sh <hf-username> [space-name] [branch]
#   e.g. deploy/push-to-huggingface.sh sahilijaz nexus-api dev
#
# Git prompts for credentials: username = your HF username,
# password = a WRITE access token from https://huggingface.co/settings/tokens
set -euo pipefail

HF_USER=${1:?usage: $0 <hf-username> [space-name] [branch]}
SPACE=${2:-nexus-api}
BRANCH=${3:-$(git rev-parse --abbrev-ref HEAD)}
REMOTE=hf
URL="https://huggingface.co/spaces/${HF_USER}/${SPACE}"

cd "$(dirname "$0")/.."

if ! git remote get-url "$REMOTE" >/dev/null 2>&1; then
  git remote add "$REMOTE" "$URL"
else
  git remote set-url "$REMOTE" "$URL"
fi

# Spaces must never receive secrets or local state; both are gitignored,
# but fail loudly if someone force-added them.
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  echo "ERROR: .env is tracked by git — remove it before pushing to a public Space." >&2
  exit 1
fi

echo "==> Pushing ${BRANCH} to ${URL} (Spaces build from branch 'main')"
git push "$REMOTE" "${BRANCH}:main" --force-with-lease

cat <<EOF

Pushed. The Space is building now (first build ~5-8 min, Chromium included):
  ${URL}            (Logs tab shows the build)
  https://${HF_USER}-${SPACE}.hf.space/health   (once it is up)

Secrets go in Settings -> Variables and secrets (see deploy/huggingface/README.md).
EOF
