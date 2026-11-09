#!/usr/bin/env bash
set -euo pipefail

CERT_DIR="${1:-certs}"
DAYS="${2:-365}"
CN="${3:-localhost}"

mkdir -p "$CERT_DIR"

openssl req -x509 -newkey rsa:2048 \
  -keyout "$CERT_DIR/key.pem" \
  -out "$CERT_DIR/cert.pem" \
  -days "$DAYS" -nodes \
  -subj "/CN=$CN" \
  -addext "subjectAltName=DNS:$CN,DNS:localhost,IP:127.0.0.1" \
  2>/dev/null

echo "Self-signed certificate generated in $CERT_DIR/ (valid $DAYS days, CN=$CN)"
echo "  cert: $CERT_DIR/cert.pem"
echo "  key:  $CERT_DIR/key.pem"
