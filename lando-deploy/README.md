# Lando Deploy Webhook

`deploy.timkley.dev` ist der zentrale Deploy-Einstieg für Laravel-/FrankenPHP-Apps auf `lando`.

Der Ansatz ist bewusst kein SSH aus GitHub Actions und kein self-hosted Runner. GitHub-hosted Runner holen sich stattdessen ein kurzlebiges GitHub-OIDC-Token und rufen den HTTPS-Webhook auf. Der Dienst auf `lando` verifiziert das Token gegen GitHubs OIDC-Keys und startet bei migrierten Workflows das lokale `deploy.sh` mit dem exakt geprüften Commit.

## Ablauf

1. Pushes auf `main` und manuelle Runs starten denselben CI-Workflow.
2. Der `deploy`-Job hängt mit `needs: tests` vom erfolgreichen Test-Job ab.
3. Der Workflow fordert mit `id-token: write` ein OIDC-Token mit Audience `lando-deploy` an.
4. Der Workflow verwendet `github.sha`, also exakt den Commit, den der Test-Job geprüft hat, und ruft `https://deploy.timkley.dev/deploy` mit `Authorization: Bearer <oidc-token>` und einem JSON-Payload auf.
5. `lando-deploy-webhook.service` prüft:
   - Issuer: `https://token.actions.githubusercontent.com`
   - Audience: `lando-deploy`
   - Repository ist in `/home/admin/lando-deploy-webhook/config.json` erlaubt
   - Ref ist erlaubt, normalerweise `refs/heads/main`
   - Payload-Repository, -Ref und -Event stimmen exakt mit den OIDC-Claims überein
   - Payload-SHA ist ein vollständiger Commit-SHA und stimmt mit dem signierten OIDC-`sha` überein
   - Payload-`workflow_ref` stimmt mit dem signierten OIDC-Claim überein und ist für die App freigegeben
6. Bei einem vollständigen Payload nimmt der Dienst pro App einen Server-Lock, checkt den SHA aus und führt `/var/www/<app>/deploy.sh <sha>` aus.
7. Das Script prüft bei einem vollständigen Payload, dass der SHA in der `origin/main`-Historie liegt, checkt ihn detached aus und prüft abschließend `/up` sowie den aktiven SHA.
8. Bei erfolgreichem Deploy sendet der Dienst optional eine Discord-Benachrichtigung, wenn `LANDO_DEPLOY_DISCORD_WEBHOOK_URL` gesetzt ist.

> Bestehende Workflows mit einem leeren Payload bleiben vorübergehend kompatibel: Der Dienst startet das vorhandene `deploy.sh` ohne Argument und ohne zentralen Checkout. Der `sha`-Claim und der aktive SHA werden nur informativ geloggt. Erst ein vollständiger Payload aktiviert die revisionsfeste Ausführung; neue Workflows müssen deshalb migriert werden.

## Server-Dateien

Auf `lando`:

```text
/home/admin/lando-deploy-webhook/server.py
/home/admin/lando-deploy-webhook/config.json
/home/admin/lando-deploy-webhook/secrets.env
/etc/systemd/system/lando-deploy-webhook.service
/home/admin/docker/traefik/dynamic/lando-deploy.toml
```

In diesem Repo:

```text
lando-deploy/server.py
lando-deploy/config.example.json
lando-deploy/lando-deploy-webhook.service
traefik/dynamic/lando-deploy.toml
```

## Betrieb

Status prüfen:

```bash
ssh lando 'systemctl status lando-deploy-webhook.service --no-pager'
curl -fsS https://deploy.timkley.dev/health
```

Logs lesen:

```bash
ssh lando 'sudo journalctl -u lando-deploy-webhook.service --no-pager -n 100'
```

Dienst nach Änderung neu laden:

```bash
scp lando-deploy/server.py lando:/tmp/server.py
scp lando-deploy/lando-deploy-webhook.service lando:/tmp/lando-deploy-webhook.service
scp lando-deploy/config.example.json lando:/tmp/config.json
ssh lando '
  set -euo pipefail
  mv -f /tmp/server.py /home/admin/lando-deploy-webhook/server.py
  mv -f /tmp/config.json /home/admin/lando-deploy-webhook/config.json
  sudo mv -f /tmp/lando-deploy-webhook.service /etc/systemd/system/lando-deploy-webhook.service
  chmod 700 /home/admin/lando-deploy-webhook/server.py
  chmod 600 /home/admin/lando-deploy-webhook/config.json
  sudo chown root:root /etc/systemd/system/lando-deploy-webhook.service
  sudo systemctl daemon-reload
  sudo systemctl restart lando-deploy-webhook.service
'
```

Discord-Deployment-Benachrichtigung konfigurieren:

```bash
ssh lando '
  set -euo pipefail
  install -m 600 -o admin -g admin /dev/null /home/admin/lando-deploy-webhook/secrets.env
  editor /home/admin/lando-deploy-webhook/secrets.env
  sudo systemctl restart lando-deploy-webhook.service
'
```

`secrets.env` enthält nicht versionierte Werte:

```dotenv
LANDO_DEPLOY_DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
```

Traefik-Route deployen:

```bash
scp traefik/dynamic/lando-deploy.toml lando:/tmp/lando-deploy.toml
ssh lando '
  sudo mv -f /tmp/lando-deploy.toml /home/admin/docker/traefik/dynamic/lando-deploy.toml
  sudo chown admin:admin /home/admin/docker/traefik/dynamic/lando-deploy.toml
'
```

## Neues Repo anbinden

1. Das App-Repo muss auf `lando` unter `/var/www/<app>` liegen.
2. Dort muss ein ausführbares `/var/www/<app>/deploy.sh` existieren.
3. `/home/admin/lando-deploy-webhook/config.json` um das Repo und bei einem migrierten Workflow um dessen `workflow_refs` ergänzen:

```json
{
  "apps": {
    "timkley/example": {
      "app": "example",
      "refs": ["refs/heads/main"],
      "events": ["push", "workflow_dispatch"],
      "workflow_refs": [
        "timkley/example/.github/workflows/ci.yml@refs/heads/main"
      ]
    }
  }
}
```

4. Dienst neu starten:

```bash
ssh lando 'sudo systemctl restart lando-deploy-webhook.service'
```

5. Den Deploy-Job in den bestehenden CI-Workflow aufnehmen. Der Test-Job muss `tests` heißen:

```yaml
name: tests

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: production-${{ github.repository }}
  cancel-in-progress: false

permissions:
  contents: read

jobs:
  tests:
    # Bestehender Test-Job des App-Repos.
    runs-on: ubuntu-latest
    steps: []

  deploy:
    if: github.event_name == 'push' || github.event_name == 'workflow_dispatch'
    needs: tests
    runs-on: ubuntu-latest
    environment: production
    timeout-minutes: 20
    permissions:
      contents: read
      id-token: write

    steps:
      - name: Deploy through Lando webhook
        env:
          DEPLOY_URL: https://deploy.timkley.dev/deploy
          OIDC_AUDIENCE: lando-deploy
          DEPLOY_REPOSITORY: ${{ github.repository }}
          DEPLOY_REF: ${{ github.ref }}
          DEPLOY_EVENT: ${{ github.event_name }}
          DEPLOY_SHA: ${{ github.sha }}
          DEPLOY_WORKFLOW_REF: ${{ github.workflow_ref }}
          DEPLOY_RUN_ID: ${{ github.run_id }}
        run: |
          if [[ "${DEPLOY_REF}" != "refs/heads/main" ]]; then
            echo "Deployment ref is not allowed: ${DEPLOY_REF}" >&2
            exit 1
          fi

          if [[ ! "${DEPLOY_SHA}" =~ ^[0-9a-fA-F]{40}$ ]]; then
            echo "Deployment SHA is missing or invalid: ${DEPLOY_SHA}" >&2
            exit 1
          fi

          oidc_token="$(
            curl -fsSL \
              -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
              "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${OIDC_AUDIENCE}" \
              | jq -r '.value'
          )"

          payload="$(
            jq -cn \
              --arg repository "${DEPLOY_REPOSITORY}" \
              --arg ref "${DEPLOY_REF}" \
              --arg event "${DEPLOY_EVENT}" \
              --arg sha "${DEPLOY_SHA}" \
              --arg workflow_ref "${DEPLOY_WORKFLOW_REF}" \
              --arg run_id "${DEPLOY_RUN_ID}" \
              '{repository: $repository, ref: $ref, event: $event, sha: $sha, workflow_ref: $workflow_ref, run_id: $run_id}'
          )"

          curl --fail-with-body -sSL \
            -X POST \
            -H "Authorization: Bearer ${oidc_token}" \
            -H "Content-Type: application/json" \
            --data "${payload}" \
            "$DEPLOY_URL"
```

## Sicherheit

- Keine GitHub-Secrets pro Repo.
- Keine GitHub-hosted Runner im Tailscale-Netz.
- Kein öffentlicher SSH-Zugang für Deploys.
- Discord-Webhook-URLs liegen in `/home/admin/lando-deploy-webhook/secrets.env`, nicht im Git-Repo.
- Der interne Dienst-Port `8010` ist per UFW nur aus dem Docker-Netz erreichbar.
- Traefik routet nur exakt `/deploy` und `/health` an den Dienst.
- Ein GitHub-OIDC-Token ist kurzlebig und an Repository, Ref, Event und Audience gebunden.
- Neue Repos müssen explizit in der Allowlist stehen.

## Quellen

- GitHub OIDC: https://docs.github.com/en/actions/reference/openid-connect-reference
- GitHub Deployments mit Actions: https://docs.github.com/en/actions/concepts/use-cases/deploying-with-github-actions
