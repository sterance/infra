# Rendered by bootstrap.sh — edit the template in the infra repo, not this copy.
ui            = true
disable_mlock = true            # recommended with integrated raft storage
api_addr      = "${BAO_ADDR}"
cluster_addr  = "https://openbao:8201"

storage "raft" {
  path    = "/openbao/file"
  node_id = "${BAO_NODE_ID}"
}

# Plain HTTP: only cloudflared (edge network) and 127.0.0.1 on the host can
# reach this port. Cloudflare terminates TLS.
listener "tcp" {
  address         = "0.0.0.0:8200"
  cluster_address = "0.0.0.0:8201"
  tls_disable     = true
  x_forwarded_for_authorized_addrs = "172.16.0.0/12"
}

# Auto-unseal from a 32-byte key file. The key never goes into backups.
seal "static" {
  current_key_id = "${KEY_ID}"
  current_key    = "file:///openbao/secrets/unseal.key"
}
