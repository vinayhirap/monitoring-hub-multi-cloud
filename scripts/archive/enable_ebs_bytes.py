#!/usr/bin/env python3
"""
enable_ebs_bytes.py

Adds VolumeReadBytes (metric_id=8) and VolumeWriteBytes (metric_id=9) to
account 5's (AuroGov Mumbai) enabled metric selection, then regenerates
the 'critical' tier YACE config.

PUT /api/account-metrics/{id} is a FULL REPLACE - it needs the complete
list of enabled_metric_ids, not just the two new ones. This script:

  1. GETs the current catalog+selection state for the account.
  2. Recursively walks the JSON (its exact shape wasn't confirmed ahead
     of time) looking for metric entries and their enabled flag, trying
     several likely key names.
  3. Prints what it found and EXITS without writing anything, unless
     you pass --apply.
  4. With --apply: sends the PUT with (existing enabled ids + 8 + 9),
     then GETs the regenerated yace-config for tier=critical and saves
     it locally so you can diff it before pushing to the server.

Usage:
    python enable_ebs_bytes.py                 # dry run - inspect only
    python enable_ebs_bytes.py --apply          # actually enable + regenerate

Assumes the backend is running locally at http://127.0.0.1:8000
(adjust BASE_URL below if not).
"""
import sys
import json
import requests

BASE_URL = "http://127.0.0.1:8000"
ACCOUNT_ID = 5
NEW_METRIC_IDS = {8, 9}  # VolumeReadBytes, VolumeWriteBytes

ENABLED_KEY_CANDIDATES = ["enabled", "is_enabled", "ams_enabled", "selected"]


def find_metric_entries(node, out):
    """Recursively collect any dict that looks like a metric row (has an 'id' key
    and at least one metric_catalog-ish field like 'metric_name')."""
    if isinstance(node, dict):
        if "id" in node and "metric_name" in node:
            out.append(node)
        for v in node.values():
            find_metric_entries(v, out)
    elif isinstance(node, list):
        for item in node:
            find_metric_entries(item, out)


def detect_enabled_key(entries):
    for key in ENABLED_KEY_CANDIDATES:
        if any(key in e for e in entries):
            return key
    return None


def main():
    apply_mode = "--apply" in sys.argv

    print(f"GET {BASE_URL}/api/account-metrics/{ACCOUNT_ID}")
    resp = requests.get(f"{BASE_URL}/api/account-metrics/{ACCOUNT_ID}", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    entries = []
    find_metric_entries(data, entries)

    if not entries:
        print("Could not find any metric entries in the response (unexpected shape).")
        print("Raw response (first 2000 chars):")
        print(json.dumps(data, indent=2)[:2000])
        sys.exit(1)

    enabled_key = detect_enabled_key(entries)
    if enabled_key is None:
        print(f"Found {len(entries)} metric entries but none had any of "
              f"{ENABLED_KEY_CANDIDATES} as a key.")
        print("Here is one sample entry so we can find the real field name:")
        print(json.dumps(entries[0], indent=2))
        sys.exit(1)

    print(f"Detected enabled-flag key: '{enabled_key}'")

    enabled_ids = {e["id"] for e in entries if e.get(enabled_key)}
    target_entries = {e["id"]: e for e in entries if e["id"] in NEW_METRIC_IDS}

    print(f"\nTotal metric entries seen: {len(entries)}")
    print(f"Currently enabled ({enabled_key}=true): {len(enabled_ids)}")
    print(f"Currently enabled ids: {sorted(enabled_ids)}")

    for mid in sorted(NEW_METRIC_IDS):
        if mid in target_entries:
            cur_state = target_entries[mid].get(enabled_key)
            print(f"  metric_id {mid} ({target_entries[mid]['metric_name']}): "
                  f"currently {enabled_key}={cur_state}")
        else:
            print(f"  metric_id {mid}: not present in response at all "
                  f"(consistent with 'no selection row' from check_catalog_metrics.py)")

    new_enabled_ids = sorted(enabled_ids | NEW_METRIC_IDS)
    print(f"\nWould PUT enabled_metric_ids ({len(new_enabled_ids)} total): {new_enabled_ids}")

    if not apply_mode:
        print("\nDRY RUN ONLY - nothing was changed.")
        print("If the numbers above look right (currently-enabled count matches what "
              "you expect from the UI), re-run with --apply to actually enable "
              "VolumeReadBytes/VolumeWriteBytes and regenerate the config.")
        return

    print(f"\nPUT {BASE_URL}/api/account-metrics/{ACCOUNT_ID}")
    put_resp = requests.put(
        f"{BASE_URL}/api/account-metrics/{ACCOUNT_ID}",
        json={"enabled_metric_ids": new_enabled_ids},
        timeout=15,
    )
    put_resp.raise_for_status()
    print("PUT result:", put_resp.json())

    print(f"\nGET {BASE_URL}/api/account-metrics/{ACCOUNT_ID}/yace-config?tier=critical")
    cfg_resp = requests.get(
        f"{BASE_URL}/api/account-metrics/{ACCOUNT_ID}/yace-config",
        params={"tier": "critical", "download": "false"},
        timeout=15,
    )
    cfg_resp.raise_for_status()

    out_path = "yace-critical-regenerated.yml"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(cfg_resp.text)
    print(f"Saved regenerated config to {out_path}")

    if "VolumeReadBytes" in cfg_resp.text and "VolumeWriteBytes" in cfg_resp.text:
        print("Confirmed: VolumeReadBytes and VolumeWriteBytes are present in "
              "the regenerated config.")
    else:
        print("WARNING: VolumeReadBytes/VolumeWriteBytes still not found in the "
              "regenerated config text - something else is going on, paste "
              f"{out_path} back to me.")

    print("\nNext (manual, on the YACE server):")
    print("  1. diff the new file against /etc/yace/config-critical.yml")
    print("  2. scp it over, then: sudo systemctl restart yace-critical")
    print("  3. re-run verify_ebs_metrics.py after a few minutes to confirm data flows")


if __name__ == "__main__":
    main()
