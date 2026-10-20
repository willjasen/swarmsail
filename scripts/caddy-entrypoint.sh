#!/bin/sh
set -eu

secret_file=/run/secrets/crowdsec-bouncer-api-key
if [ -e "$secret_file" ]; then
    if [ ! -r "$secret_file" ]; then
        echo "CrowdSec bouncer API key secret is not readable" >&2
        exit 1
    fi
    CROWDSEC_API_KEY=$(cat "$secret_file")
    if [ -z "$CROWDSEC_API_KEY" ]; then
        echo "CrowdSec bouncer API key secret is empty" >&2
        exit 1
    fi
    export CROWDSEC_API_KEY
fi

if [ "$#" -eq 0 ]; then
    set -- caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
elif [ "$1" != "caddy" ]; then
    set -- caddy "$@"
fi

exec "$@"
