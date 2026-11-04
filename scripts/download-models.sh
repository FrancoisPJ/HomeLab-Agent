#!/usr/bin/env bash
# Download the GGUF files listed under `models.items` of config.yaml from Hugging Face,
# then verify their SHA256 when one is given. Nothing is downloaded at image build time.
#
#   ./scripts/download-models.sh                # uses ./config.yaml, writes to ./models
#   CONFIG=other.yaml MODELS_DIR=/srv/models ./scripts/download-models.sh
#   HF_TOKEN=...  for gated repositories (read from .env if present)
set -euo pipefail

CONFIG="${CONFIG:-config.yaml}"
MODELS_DIR="${MODELS_DIR:-./models}"
HF_BASE="${HF_BASE:-https://huggingface.co}"

if [[ -f .env && -z "${HF_TOKEN:-}" ]]; then
  HF_TOKEN="$(grep -E '^HF_TOKEN=' .env | cut -d= -f2- || true)"
fi

list_models() {
  if command -v uv >/dev/null 2>&1; then
    uv run --quiet homelab-agent -c "$CONFIG" models
  elif command -v homelab-agent >/dev/null 2>&1; then
    homelab-agent -c "$CONFIG" models
  else
    docker run --rm -v "$PWD/$CONFIG:/config/config.yaml:ro" homelab-agent:local -c /config/config.yaml models
  fi
}

mkdir -p "$MODELS_DIR"
status=0
while IFS=$'\t' read -r repo file sha; do
  [[ -z "$repo" ]] && continue
  dest="$MODELS_DIR/$(basename "$file")"
  if [[ -f "$dest" ]]; then
    echo "✓ $dest already present"
  else
    echo "↓ $repo/$file"
    auth=()
    [[ -n "${HF_TOKEN:-}" ]] && auth=(-H "Authorization: Bearer ${HF_TOKEN}")
    curl -fL --retry 3 --continue-at - "${auth[@]}" -o "$dest.part" "$HF_BASE/$repo/resolve/main/$file"
    mv "$dest.part" "$dest"
  fi
  if [[ "$sha" != "-" ]]; then
    actual="$(sha256sum "$dest" | cut -d' ' -f1)"
    if [[ "$actual" == "$sha" ]]; then
      echo "  sha256 OK"
    else
      echo "  ✗ sha256 mismatch for $dest (expected $sha, got $actual)" >&2
      status=1
    fi
  else
    echo "  (no sha256 in config: not verified)"
  fi
done < <(list_models)
exit $status
