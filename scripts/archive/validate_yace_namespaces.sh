#!/bin/bash
# validate_yace_namespaces.sh
#
# Tests every namespace passed as an argument against the REAL installed
# yace binary (ground truth for this exact version, 0.67.0, rather than
# guessing from docs which drift between versions). For each namespace,
# generates a minimal one-job test config and starts yace against it,
# checking within ~2s whether it exits with "Service is not in known
# list" (config-parse-time failure) vs starts normally (valid namespace,
# even if it then finds zero resources - that's fine, different error).
#
# Usage:
#   ./validate_yace_namespaces.sh AWS/ApiGateway AWS/AutoScaling AWS/DAX ...
#
# Run this on the YACE server (3.109.181.40), not locally - it needs
# the actual yace binary at /usr/local/bin/yace.

BIN=/usr/local/bin/yace
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

printf "%-25s %-10s\n" "namespace" "result"
printf -- "----------------------------------------\n"

for ns in "$@"; do
  cfg="$TMPDIR/test.yml"
  cat > "$cfg" <<EOF
apiVersion: v1alpha1
discovery:
  jobs:
  - type: $ns
    regions:
    - ap-south-1
    period: 300
    length: 300
    metrics:
    - name: DummyMetric
      statistics:
      - Average
EOF

  out=$(timeout 3 "$BIN" --config.file="$cfg" 2>&1)

  if echo "$out" | grep -q "not in known list"; then
    printf "%-25s %-10s\n" "$ns" "UNSUPPORTED"
  elif echo "$out" | grep -qi "couldn't read"; then
    printf "%-25s %-10s\n" "$ns" "CONFIG_ERROR (other)"
  else
    printf "%-25s %-10s\n" "$ns" "ok"
  fi
done
