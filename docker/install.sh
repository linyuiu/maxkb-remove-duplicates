#!/usr/bin/env bash
# Compatibility entry point; the implementation and template live in install/.
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
exec bash "$SCRIPT_DIR/../install/install.sh" "$@"
