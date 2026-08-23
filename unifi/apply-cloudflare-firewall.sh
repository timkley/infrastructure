#!/usr/bin/env bash

set -euo pipefail

readonly chain="UNIFI-CLOUDFLARE"
readonly public_interface="${UNIFI_PUBLIC_INTERFACE:-eth0}"
readonly source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly ranges_file="${source_dir}/cloudflare-ips-v4.txt"

if [[ ! -r "$ranges_file" ]]; then
    echo "Cloudflare IPv4 ranges are not readable: $ranges_file" >&2
    exit 1
fi

if ! iptables -w -n -L DOCKER-USER >/dev/null 2>&1; then
    echo "Docker firewall chain DOCKER-USER is unavailable" >&2
    exit 1
fi

iptables -w -N "$chain" 2>/dev/null || true
iptables -w -F "$chain"

while IFS= read -r cidr; do
    [[ -z "$cidr" || "$cidr" == \#* ]] && continue
    iptables -w -A "$chain" -s "$cidr" -j RETURN
done < "$ranges_file"

iptables -w -A "$chain" -j DROP

if ! iptables -w -C DOCKER-USER \
    -i "$public_interface" \
    -p tcp \
    -m conntrack --ctorigdstport 8080 \
    -j "$chain" 2>/dev/null; then
    iptables -w -I DOCKER-USER 1 \
        -i "$public_interface" \
        -p tcp \
        -m conntrack --ctorigdstport 8080 \
        -j "$chain"
fi
