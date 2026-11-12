#!/bin/bash
set -euo pipefail
installer_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if ! command -v python3 >/dev/null; then
    echo 'Нужен Python 3.10 или новее.' >&2
    exit 1
fi
exec python3 "$installer_dir/install.py" "$@"
