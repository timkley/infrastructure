ui = true

storage "raft" {
  path    = "/openbao/data"
  node_id = "openbao-lando"
}

audit "file" "file" {
  description = "Lando OpenBao audit log"

  options {
    file_path = "/openbao/audit/openbao-audit.log"
    log_raw   = "false"
  }
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = true
}

api_addr     = "http://127.0.0.1:8200"
cluster_addr = "http://openbao-app:8201"
