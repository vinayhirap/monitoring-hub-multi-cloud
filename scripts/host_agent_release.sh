#!/usr/bin/env bash
# scripts/host_agent_release.sh -- stage a host agent release for nginx and sign it. Run on a BUILD / ADMIN machine,
# NEVER on the hub: the signing key must not live on a server that serves the files.
#
#   scripts/host_agent_release.sh stage  <version> <binaries_dir> <signing_key.pem> [out_dir]
#       <binaries_dir> holds cloudops-agent-<os>-<arch> files (and optionally install.sh). Writes
#       <out_dir>/<version>/{binaries, install.sh, SHA256SUMS, SHA256SUMS.sig} and <out_dir>/manifest.json(+.sig).
#   scripts/host_agent_release.sh verify <version> <release.pub> [out_dir]
#       re-checks the signatures and every checksum in a staged release.
#
# Signature: raw Ed25519 over the exact bytes of SHA256SUMS (and manifest.json), made with OpenSSL >= 3.0
# (`openssl pkeyutl -rawin`). The agent verifies the same bytes with Go's stdlib crypto/ed25519 (no dependencies).
# Generate the key once, offline:  openssl genpkey -algorithm ed25519 -out release.key.pem
#                                  openssl pkey -in release.key.pem -pubout -out release.pub.pem
set -euo pipefail
cmd="${1:-}"; version="${2:-}"
die() { echo "error: $*" >&2; exit 1; }
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[A-Za-z0-9.]+)?$ ]] || die "version must look like 1.2.3"
command -v openssl >/dev/null && openssl version | grep -q 'OpenSSL 3\|OpenSSL [4-9]' || die "OpenSSL 3.x is required for Ed25519 -rawin"

case "$cmd" in
  stage)
    src="${3:?binaries dir}"; key="${4:?signing key (pem)}"; out="${5:-./host-agent-dist}"
    [ -d "$src" ] || die "no such dir: $src"; [ -f "$key" ] || die "no such key: $key"
    dir="$out/$version"
    [ -e "$dir/SHA256SUMS" ] && die "$dir already released; versions are immutable (bump the version)"
    shopt -s nullglob
    bins=("$src"/cloudops-agent-*-*)
    [ ${#bins[@]} -gt 0 ] || die "no cloudops-agent-<os>-<arch> binaries in $src"
    mkdir -p "$dir"; cp "${bins[@]}" "$dir/"
    [ -f "$src/install.sh" ] && cp "$src/install.sh" "$dir/"
    ( cd "$dir" && sha256sum cloudops-agent-*-* $( [ -f install.sh ] && echo install.sh ) | sort -k2 > SHA256SUMS )
    openssl pkeyutl -sign -rawin -inkey "$key" -in "$dir/SHA256SUMS" -out "$dir/SHA256SUMS.sig"
    # manifest.json: what the agent polls. Only ever points at a version whose SHA256SUMS is signed above.
    { printf '{"latest":"%s","min_version":"%s","released_at":"%s","sha256sums":"%s/SHA256SUMS"}\n' \
        "$version" "${HOST_AGENT_MIN_VERSION:-$version}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$version"; } > "$out/manifest.json.new"
    mv "$out/manifest.json.new" "$out/manifest.json"
    openssl pkeyutl -sign -rawin -inkey "$key" -in "$out/manifest.json" -out "$out/manifest.json.sig"
    echo "staged $dir ($(wc -l < "$dir/SHA256SUMS") files); copy $out to the hub's /opt/monitoring-hub/host-agent-dist/"
    ;;
  verify)
    pub="${3:?public key (pem)}"; out="${4:-./host-agent-dist}"; dir="$out/$version"
    for f in "$dir/SHA256SUMS" "$out/manifest.json"; do
      openssl pkeyutl -verify -rawin -pubin -inkey "$pub" -in "$f" -sigfile "$f.sig" >/dev/null || die "BAD signature: $f"
    done
    ( cd "$dir" && sha256sum -c SHA256SUMS ) || die "checksum mismatch in $dir"
    echo "OK $version"
    ;;
  *) sed -n 2,14p "$0"; exit 2 ;;
esac
