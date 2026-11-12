#!/bin/bash
# Download a release into a disposable directory; keep installed files/backups.
set -euo pipefail
repository="${LANG_SWITCHER_REPOSITORY:-}"
version=latest
installer_args=()
while (($#)); do
    case "$1" in
        --repo) repository="${2:?Укажите owner/repo}"; shift 2 ;;
        --version) version="${2:?Укажите v1.0.0}"; shift 2 ;;
        --check-only|--offline) installer_args+=("$1"); shift ;;
        -h|--help)
            printf '%s\n' 'bootstrap.sh --repo OWNER/lang-switcher [--version v1.0.0] [--check-only] [--offline]'
            exit 0 ;;
        *) printf '%s\n' "Неизвестный аргумент: $1" >&2; exit 2 ;;
    esac
done
if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    echo 'Укажите --repo OWNER/lang-switcher.' >&2; exit 2
fi
if [[ "$version" != latest && ! "$version" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo 'Версия должна иметь вид v1.0.0.' >&2; exit 2
fi
for command in curl python3 sha256sum; do
    command -v "$command" >/dev/null || { echo "Нужна команда $command" >&2; exit 1; }
done
workdir="$(mktemp -d "${TMPDIR:-/tmp}/lang-switcher.XXXXXXXX")"
cleanup() { rm -rf -- "$workdir"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
base="https://github.com/$repository/releases/latest/download"
if [[ "$version" != latest ]]; then
    base="https://github.com/$repository/releases/download/$version"
fi
curl --fail --show-error --location --retry 2 --proto '=https' "$base/lang-switcher.tar.gz" -o "$workdir/lang-switcher.tar.gz"
curl --fail --show-error --location --retry 2 --proto '=https' "$base/lang-switcher.tar.gz.sha256" -o "$workdir/checksum"
read -r expected_hash rest < "$workdir/checksum"
[[ "$expected_hash" =~ ^[0-9a-f]{64}$ ]] || { echo 'Некорректная контрольная сумма.' >&2; exit 1; }
(cd -- "$workdir"; printf '%s  lang-switcher.tar.gz\n' "$expected_hash" | sha256sum --check --status -)
python3 - "$workdir/lang-switcher.tar.gz" "$workdir" <<'PY'
from pathlib import PurePosixPath
import sys
import tarfile
with tarfile.open(sys.argv[1], 'r:gz') as archive:
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts or
                path.parts[0] != 'lang-switcher' or not (member.isfile() or member.isdir())):
            raise SystemExit('Недопустимый путь или тип файла в архиве')
    # All paths and types were checked, including a ban on symlinks/hardlinks.
    archive.extractall(sys.argv[2])
PY
bash "$workdir/lang-switcher/install.sh" --repository "$repository" "${installer_args[@]}"
