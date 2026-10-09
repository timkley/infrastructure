# Paperless object delete permissions

This maintenance task is independent of the retired `heisenberg-access-mcp`.
Holocron uses the same restricted private API user, so its existing direct
object delete grants still need maintenance when document permissions change.

The script, tests and units moved here unchanged on 2026-10-09. The separately
installed host script remains at
`/usr/local/lib/heisenberg-paperless-delete-permissions/sync-paperless-delete-permissions.py`.
The existing `heisenberg-paperless-delete-permissions@private.timer` stays enabled;
its historical name does not imply a dependency on the removed MCP container.
The ledger stays in `/var/lib/heisenberg-paperless-delete-permissions`.

The command previews by default. It grants only direct `delete_document` rights
for active foreign-owned documents that already have effective view/change
rights, records only its own grants and never changes global permissions.
The server timer runs the bounded existing `--source private --apply` operation.
No API token or OpenBao access is used.

Validation:

```sh
python3 -m unittest discover -s paperless/delete-permissions/tests -v
ssh lando 'systemctl is-enabled heisenberg-paperless-delete-permissions@private.timer'
ssh lando 'systemctl is-active heisenberg-paperless-delete-permissions@private.timer'
```

The separate Work-MCP keeps its own maintained copies. Its deployment and data
are unaffected by retirement of the private Lando MCP.
