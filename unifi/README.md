# UniFi on Lando

The adopted access points use `http://unifi.timkley.dev:8080/inform` through
Cloudflare. The app publishes only the ports required for that connection:

- `8080/tcp` for device inform traffic, restricted to Cloudflare IPv4 source
  networks by `unifi-cloudflare-firewall.service`.
- `3478/udp` for STUN, which does not use Cloudflare's HTTP proxy.

The admin UI and API use Traefik on HTTPS port 443. Guest portal, speed test,
and layer-2 discovery ports are not published. For remote adoption, use the
configured inform URL. If layer-2 discovery is needed later, temporarily add
`0.0.0.0:10001:10001/udp` and remove it again after adoption.

The tracked Cloudflare ranges come from <https://www.cloudflare.com/ips-v4>.
Compare that source with `cloudflare-ips-v4.txt` during maintenance. Apply the
firewall after Docker is running:

```bash
sudo install -m 0644 \
  /home/admin/docker/unifi/unifi-cloudflare-firewall.service \
  /etc/systemd/system/unifi-cloudflare-firewall.service
sudo systemctl daemon-reload
sudo systemctl enable --now unifi-cloudflare-firewall.service
```

Reload the rules after changing the tracked ranges:

```bash
sudo systemctl reload unifi-cloudflare-firewall.service
```
