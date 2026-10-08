#!/bin/sh
set -eu

umask 077
mkdir -p /data
if [ ! -e /data/tokens.json ]; then
    printf '{"tokens": {}}\n' > /data/tokens.json
fi

exec "$@"
