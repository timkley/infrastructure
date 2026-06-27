# Narrow OpenBao policy for heisenberg-access-mcp.
# Agents receive MCP capabilities, not raw OpenBao paths or secret values.

path "sys/health" {
  capabilities = ["read"]
}

path "sys/seal-status" {
  capabilities = ["read"]
}

path "secret/data/heisenberg/homeassistant" {
  capabilities = ["read"]
}

path "secret/data/heisenberg/freshrss" {
  capabilities = ["read"]
}

path "secret/data/heisenberg/elevenlabs" {
  capabilities = ["read"]
}

path "secret/data/heisenberg/google-health/oauth-client" {
  capabilities = ["read"]
}

path "secret/data/heisenberg/google-health/oauth-token" {
  capabilities = ["read", "update"]
}

path "secret/data/heisenberg/x/oauth" {
  capabilities = ["read", "update"]
}
