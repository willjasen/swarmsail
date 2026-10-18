#!/usr/bin/env bash
# Issue or renew the LAN Headscale certificate with Cloudflare DNS-01.
set -euo pipefail

usage() {
  printf 'Usage: %s [--apply]\n' "$0"
}

case "${1:-}" in
  '') apply=false ;;
  --apply) apply=true ;;
  --help|-h) usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac
if (( $# > 1 )); then usage >&2; exit 2; fi

data_dir="${SWARMSAIL_LOCAL_DATA_DIR:-$HOME/Library/Application Support/Swarmsail}"
token_file="$data_dir/cloudflare-dns-token"
acme_dir="$data_dir/acme"
domain=rawrarr.willjasen.net

if [[ ! -d "$data_dir" ]]; then
  printf 'Missing Swarmsail data directory: %s\n' "$data_dir" >&2
  exit 1
fi
printf 'Certificate: %s\nACME state: %s\nCloudflare token file: %s\n' "$domain" "$acme_dir" "$token_file"
if [[ "$apply" == false ]]; then
  printf 'Dry run: would use the token file with lego DNS-01; no changes made.\n'
  exit 0
fi
if [[ ! -f "$token_file" || ! -s "$token_file" ]]; then
  printf 'Cloudflare token file is missing or empty.\n' >&2
  exit 1
fi
if [[ "$(stat -f %Lp "$token_file")" != 600 ]]; then
  printf 'Cloudflare token file must have mode 600.\n' >&2
  exit 1
fi
command -v docker >/dev/null || { printf 'docker is required.\n' >&2; exit 1; }
install -d -m 700 "$acme_dir"
docker run --rm \
  --user "$(id -u):$(id -g)" \
  --mount "type=bind,src=$acme_dir,dst=/acme" \
  --mount "type=bind,src=$token_file,dst=/run/secrets/cloudflare-dns-token,readonly" \
  -e CF_DNS_API_TOKEN_FILE=/run/secrets/cloudflare-dns-token \
  goacme/lego:v5.5.2 \
  --path /acme --server letsencrypt --dns cloudflare \
  --domains "$domain" --accept-tos run
cert="$acme_dir/certificates/$domain.crt"
key="$acme_dir/certificates/$domain.key"
[[ -s "$cert" && -s "$key" ]] || { printf 'Certificate or key missing after lego run.\n' >&2; exit 1; }
chmod 600 "$key"
openssl x509 -in "$cert" -noout -checkend 2592000 >/dev/null || {
  printf 'Certificate expires within 30 days.\n' >&2; exit 1;
}
printf 'Certificate issued or renewed and checked: %s\n' "$cert"
