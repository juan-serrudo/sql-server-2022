#!/usr/bin/env bash
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v python3 >/dev/null 2>&1 || { echo "ERROR: instale python3 antes de continuar." >&2; exit 127; }
exec python3 "$PROJECT_ROOT/scripts/historico.py" sql "$@"
