#!/bin/sh
# Run the test suite on Debian's packaged dependencies → SETUP.md#dependency-policy.
# Every dependency comes from apt except txstore, which Debian does not package yet: it is
# installed from PyPI at the uv.lock version and wheel hash, without its dependencies
# → ROADMAP.md#known-deviations.
# debkit itself installs with no index.
set -eu

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
    python3 python3-venv python3-pip python3-hatchling gnupg zstd \
    python3-debian python3-gnupg python3-humanize python3-urllib3 \
    python3-boto3 python3-botocore python3-requests python3-pyxattr \
    python3-moto python3-flask python3-flask-cors \
    python3-pytest >/dev/null

python3 --version
txstore_version="$(awk '/^name = "txstore"$/ { getline; gsub(/[^0-9.a-z]/, "", $3); print $3 }' uv.lock)"
test -n "$txstore_version"
txstore_hash="$(awk '/^name = "txstore"$/ { found = 1 }
    found && /txstore-.*\.whl/ { match($0, /sha256:[0-9a-f]+/); print substr($0, RSTART + 7, RLENGTH - 7); exit }' uv.lock)"
test -n "$txstore_hash"
printf 'txstore==%s --hash=sha256:%s\n' "$txstore_version" "$txstore_hash" >/tmp/txstore-requirements.txt
python3 -m venv --system-site-packages /tmp/venv
/tmp/venv/bin/pip install --quiet --no-deps --require-hashes -r /tmp/txstore-requirements.txt
/tmp/venv/bin/pip install --quiet --no-deps --no-index --no-build-isolation .
/tmp/venv/bin/python -m pytest -q -rs -p no:cacheprovider -o addopts=""
