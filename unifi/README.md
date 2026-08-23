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

## Update and migration

The stack uses UniFi Network Application 10.5.67 and MongoDB 8.0.29. Both
images are pinned by tag and digest. MongoDB authentication is enabled and the
database port is not published.

The former `jacobalberty/unifi` installation cannot be updated in place. Its
data remains in the old Docker volumes for rollback. Move to this stack by
restoring a fresh UniFi backup into the new, empty `config` and `mongodb`
volumes:

1. In the running controller, create and download a full backup including
   history.
2. Copy `.env.example` to `.env`. Generate two different secrets with
   `openssl rand -hex 32` and set mode `0600` on `.env`.
3. Stop the old stack without deleting volumes: `docker compose down`.
4. Start the new stack: `docker compose up -d`.
5. Open the setup page and choose **Restore Server from a Backup**.
6. Confirm that the UI, both access points, `/inform`, STUN, and the firewall
   service work before removing any old volume.

Do not run `docker compose down -v` during the migration or rollback window.
The init script runs only when the `mongodb` volume is empty.

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
