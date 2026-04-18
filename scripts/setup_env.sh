#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Creating virtual environment..."
python3 -m venv .venv
source .venv/bin/activate

echo "==> Upgrading pip..."
pip install --upgrade pip --quiet

echo "==> Installing base dependencies..."
pip install -e "." --quiet

echo "==> Installing dev dependencies..."
pip install -e ".[dev]" --quiet

echo "==> Copying .env.example -> .env (if not already present)..."
if [ ! -f .env ]; then
    cp .env.example .env
    echo "    Created .env — fill in your API keys before hacking."
else
    echo "    .env already exists — skipping copy."
fi

echo "==> Verifying keys..."
python scripts/verify_keys.py || true

echo ""
echo "Setup complete. Activate with: source .venv/bin/activate"
echo "Then fill in .env and run: python scripts/verify_keys.py"
