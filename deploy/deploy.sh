#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -f .env ]]; then
  echo "Missing .env in ${SCRIPT_DIR}. Copy .env.example to .env and fill it first."
  exit 1
fi

docker compose pull
docker compose up -d --remove-orphans --wait
docker compose ps
