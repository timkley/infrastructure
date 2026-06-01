# Lando Deploy Webhook

`deploy.timkley.dev` ist der zentrale Deploy-Einstieg für Laravel-/FrankenPHP-Apps auf `lando`.

Der Ansatz ist bewusst kein SSH aus GitHub Actions und kein self-hosted Runner. GitHub-hosted Runner holen sich stattdessen ein kurzlebiges GitHub-OIDC-Token und rufen den HTTPS-Webhook auf. Der Dienst auf `lando` verifiziert das Token gegen GitHubs OIDC-Keys und startet nur für erlaubte Repository-/Branch-Kombinationen das lokale `deploy.sh`.

## Ablauf

1. Push auf `main` läuft durch den normalen CI-Workflow.
2. Der Deploy-Workflow läuft auf `ubuntu-latest`.
3. Der Workflow fordert mit `id-token: write` ein OIDC-Token mit Audience `lando-deploy` an.
4. Der Workflow ruft `https://deploy.timkley.dev/deploy` mit `Authorization: Bearer <oidc-token>` auf.
5. `lando-deploy-webhook.service` prüft:
   - Issuer: `https://token.actions.githubusercontent.com`
   - Audience: `lando-deploy`
   - Repository ist in `/home/admin/lando-deploy-webhook/config.json` erlaubt
   - Ref ist erlaubt, normalerweise `refs/heads/main`
6. Der Dienst führt `/var/www/<app>/deploy.sh` aus.

## Server-Dateien

Auf `lando`:

```text
/home/admin/lando-deploy-webhook/server.py
/home/admin/lando-deploy-webhook/config.json
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
scp lando-deploy/config.example.json lando:/tmp/config.json
ssh lando '
  set -euo pipefail
  mv -f /tmp/server.py /home/admin/lando-deploy-webhook/server.py
  mv -f /tmp/config.json /home/admin/lando-deploy-webhook/config.json
  chmod 700 /home/admin/lando-deploy-webhook/server.py
  chmod 600 /home/admin/lando-deploy-webhook/config.json
  sudo systemctl restart lando-deploy-webhook.service
'
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
3. `/home/admin/lando-deploy-webhook/config.json` um das Repo ergänzen:

```json
{
  "apps": {
    "timkley/example": {
      "app": "example",
      "refs": ["refs/heads/main"],
      "events": ["workflow_run", "workflow_dispatch"]
    }
  }
}
```

4. Dienst neu starten:

```bash
ssh lando 'sudo systemctl restart lando-deploy-webhook.service'
```

5. Workflow ins App-Repo kopieren:

```yaml
name: deploy

on:
  workflow_run:
    workflows: ["tests"]
    types: [completed]
    branches: [main]
  workflow_dispatch:

concurrency:
  group: production-${{ github.repository }}
  cancel-in-progress: false

permissions:
  contents: read
  id-token: write

jobs:
  deploy:
    if: github.event_name == 'workflow_dispatch' || github.event.workflow_run.conclusion == 'success'
    runs-on: ubuntu-latest
    environment: production
    timeout-minutes: 20

    steps:
      - name: Deploy through Lando webhook
        env:
          DEPLOY_URL: https://deploy.timkley.dev/deploy
          OIDC_AUDIENCE: lando-deploy
        run: |
          oidc_token="$(
            curl -fsSL \
              -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
              "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${OIDC_AUDIENCE}" \
              | jq -r '.value'
          )"

          curl --fail-with-body -sSL \
            -X POST \
            -H "Authorization: Bearer ${oidc_token}" \
            -H "Content-Type: application/json" \
            --data '{}' \
            "$DEPLOY_URL"
```

## Sicherheit

- Keine GitHub-Secrets pro Repo.
- Keine GitHub-hosted Runner im Tailscale-Netz.
- Kein öffentlicher SSH-Zugang für Deploys.
- Der interne Dienst-Port `8010` ist per UFW nur aus dem Docker-Netz erreichbar.
- Traefik routet nur exakt `/deploy` und `/health` an den Dienst.
- Ein GitHub-OIDC-Token ist kurzlebig und an Repository, Ref, Event und Audience gebunden.
- Neue Repos müssen explizit in der Allowlist stehen.

## Quellen

- GitHub OIDC: https://docs.github.com/en/actions/reference/openid-connect-reference
- GitHub Deployments mit Actions: https://docs.github.com/en/actions/concepts/use-cases/deploying-with-github-actions
