#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "$(uname -s)" != "Darwin" || "$(uname -m)" != "arm64" ]]; then
    echo "This local bundle requires macOS ARM64." >&2
    exit 1
fi

if [[ ! -x .venv/bin/python ]]; then
    command -v python3.13 >/dev/null || {
        echo "Python 3.13 is required for the bundled offline wheels." >&2
        exit 1
    }
    python3.13 -m venv .venv
fi

.venv/bin/python -m pip install --disable-pip-version-check --no-index \
    --find-links "$SCRIPT_DIR/assets/wheels" -r requirements.txt

exec .venv/bin/python server.py "$@"
