#!/usr/bin/env python3
"""
apply_region_bug_and_discovery_expansion.py
=============================================
Three things in one script:

1. FIXES THE "Unknown column 'r.region' in 'field list'" BUG.
   Root cause: db/schema.sql's `resources` table never defined `region` or
   `instance_state`, but every discovery write (runner.py) and the alert
   evaluator's read both always assumed those columns exist. Any DB
   bootstrapped strictly from the committed schema.sql + migrations (e.g.
   this dev DB) hits it on the very first discovery run. Adds
   db/migrations/002_resources_region_instance_state.sql (idempotent,
   IF NOT EXISTS) and applies it.

   Also found & fixed along the way: db/migrations/009_multi_cloud_provider_
   columns.sql was sitting at the project ROOT instead of db/migrations/ —
   another casualty of the corrupted .gitignore from before (it silently
   ignored new untracked files, so this migration never made it into the
   real migrations folder even though it did get committed by explicit
   `git add`). This script moves both 009 files into db/migrations/ where
   they belong, and — since it's uncertain whether 009's ALTERs were ever
   actually run against this DB — re-applies them here too, idempotently
   (checked against information_schema.COLUMNS first, so this is a no-op
   if they're already there). Those columns are required for the Azure/GCP
   onboarding code already on main to work at all.

2. EXPANDS AZURE + GCP DISCOVERY to a second real resource type each:
     - Azure: App Service (Web Apps) — was catalog-only ("extended" tier,
       no resources ever actually discovered for it); now actively
       discovered via azure-mgmt-web and promoted to "core" tier.
     - GCP: Cloud Run — was directory-tier only (namespace registered,
       no metrics); now actively discovered via google-cloud-run across
       the same region list used in the onboarding UI, with a real
       curated metric set, promoted to "core" tier.
   New deps: azure-mgmt-web, google-cloud-run (added to requirements.txt).

3. EMOJI-TO-SVG ICON STATUS (no code changes needed — see printed report):
   Settings.jsx, Compliance.jsx, and UserManagement.jsx are ALL already
   fully converted (verified: zero emoji in any of the three). There's a
   leftover `emoji-to-icons-update/` staging folder at the repo root from
   an earlier session — do not merge it: it predates the console-url
   consolidation refactor and would regress ServiceDetail.jsx back to
   hardcoded AWS-only console links. Safe to delete that folder. The only
   remaining "📊" was inside a source code COMMENT in ServiceDetail.jsx
   (never rendered) — this script rewords it. The one actually-rendered
   emoji using the same icon (the Alerts page's "📊 Metrics" button) is
   OUTSIDE the four pages that were ever flagged as in-progress — flagging
   it here rather than silently expanding scope; say the word and it's a
   quick follow-up.

Usage:
    python apply_region_bug_and_discovery_expansion.py --dry-run
    python apply_region_bug_and_discovery_expansion.py
"""
import argparse
import base64
import os
import shutil
import sys
from pathlib import Path


FILES = {
    'db/migrations/002_resources_region_instance_state.sql': {"new": True, "b64": """
LS0gZGIvbWlncmF0aW9ucy8wMDJfcmVzb3VyY2VzX3JlZ2lvbl9pbnN0YW5jZV9zdGF0ZS5zcWwKLS0KLS0gUm9vdCBjYXVzZSBv
ZiAiVW5rbm93biBjb2x1bW4gJ3IucmVnaW9uJyBpbiAnZmllbGQgbGlzdCciOgotLSBkYi9zY2hlbWEuc3FsJ3MgQ1JFQVRFIFRB
QkxFIHJlc291cmNlcyBuZXZlciBpbmNsdWRlZCBgcmVnaW9uYCBvcgotLSBgaW5zdGFuY2Vfc3RhdGVgLCBidXQgZXZlcnkgd3Jp
dGUgcGF0aCAoYXBwL2NvbGxlY3Rvci9kaXNjb3ZlcnkvcnVubmVyLnB5Ci0tIF91cHNlcnRfcmVzb3VyY2UsIHRoZSBFQzIgaW5z
dGFuY2Utc3RhdGUgVVBEQVRFKSBhbmQgZXZlcnkgcmVhZCBwYXRoCi0tIChhcHAvY29sbGVjdG9yL2FsZXJ0X2V2YWx1YXRvci5w
eSdzIFNFTEVDVCByLnJlZ2lvbikgaGFzIGFsd2F5cyBhc3N1bWVkCi0tIGJvdGggY29sdW1ucyBleGlzdC4gVGhleSBETyBleGlz
dCBvbiBlbnZpcm9ubWVudHMgd2hlcmUgc29tZW9uZSByYW4gYW4KLS0gYWQtaG9jIGBBTFRFUiBUQUJMRSByZXNvdXJjZXMgQURE
IENPTFVNTiByZWdpb24gLi4uYCBieSBoYW5kIGF0IHNvbWUKLS0gcG9pbnQgYW5kIGl0IG5ldmVyIGdvdCB0dXJuZWQgaW50byBh
IGNvbW1pdHRlZCBtaWdyYXRpb24g4oCUIGFueQotLSBlbnZpcm9ubWVudCBib290c3RyYXBwZWQgc3RyaWN0bHkgZnJvbSBzY2hl
bWEuc3FsICsgdGhlIGNvbW1pdHRlZAotLSBtaWdyYXRpb25zIChlLmcuIGEgZnJlc2ggRUMyIHNldHVwKSBoaXRzIHRoaXMgZXJy
b3Igb24gdGhlIHZlcnkgZmlyc3QKLS0gZGlzY292ZXJ5IHJ1bi4KLS0KLS0gZGIvbWlncmF0aW9ucy9hZGRfbW9uaXRvcmluZ190
aWVyLnNxbCBhbHJlYWR5IGRlcGVuZHMgb24KLS0gYGluc3RhbmNlX3N0YXRlYCBleGlzdGluZyAoYEFERCBDT0xVTU4gbW9uaXRv
cmluZ190aWVyIC4uLiBBRlRFUgotLSBpbnN0YW5jZV9zdGF0ZWApLCBzbyB0aGlzIG1pZ3JhdGlvbiBtdXN0IHJ1biBiZWZvcmUg
dGhhdCBvbmUuCi0tCi0tIFB1cmVseSBhZGRpdGl2ZSwgTXlTUUwgOCBJRiBOT1QgRVhJU1RTIGd1YXJkcyDigJQgc2FmZSB0byBy
ZS1ydW4uCgpBTFRFUiBUQUJMRSByZXNvdXJjZXMKICAgIEFERCBDT0xVTU4gSUYgTk9UIEVYSVNUUyByZWdpb24gVkFSQ0hBUig1
MCkgREVGQVVMVCBOVUxMIEFGVEVSIHRhZ3MsCiAgICBBREQgQ09MVU1OIElGIE5PVCBFWElTVFMgaW5zdGFuY2Vfc3RhdGUgVkFS
Q0hBUigzMCkgREVGQVVMVCBOVUxMIEFGVEVSIHJlZ2lvbjsKCkFMVEVSIFRBQkxFIHJlc291cmNlcwogICAgQUREIElOREVYIElG
IE5PVCBFWElTVFMgaWR4X3Jlc291cmNlc19yZWdpb24gKGF3c19hY2NvdW50X2lkLCByZWdpb24pOwo=
"""},
    'app/providers/azure/discovery.py': {"new": False, "b64": """
IyBhcHAvcHJvdmlkZXJzL2F6dXJlL2Rpc2NvdmVyeS5weQoiIiIKUmVhbCBBenVyZSByZXNvdXJjZSBkaXNjb3ZlcnkgdmlhIGF6
dXJlLW1nbXQgU0RLcy4gTWlycm9ycwphcHAuY29sbGVjdG9yLmRpc2NvdmVyeS5ydW5uZXIncyBjb250cmFjdDogd3JpdGVzL3Vw
ZGF0ZXMgcm93cyBpbgpgcmVzb3VyY2VzYCwgc2NvcGVkIHRvIGEgc2luZ2xlIGFjY291bnQsIGNhbGxlZCBmcm9tIEF6dXJlUHJv
dmlkZXIuCgpPbmx5IHRocmVlIHJlc291cmNlIHR5cGVzIGZvciBub3cgKFZNcywgU3RvcmFnZSBBY2NvdW50cywgU1FMIERhdGFi
YXNlcykg4oCUCm1hdGNoZXMgdGhlIENPUkUgdGllciBpbiBtZXRyaWNfY2F0YWxvZ19kYXRhLkNVUkFURUQuIEV4dGVuZGluZyB0
byBtb3JlCkF6dXJlIHJlc291cmNlIHR5cGVzIGlzIGp1c3QgYWRkaW5nIGFub3RoZXIgX2Rpc2NvdmVyXyogZnVuY3Rpb24gaGVy
ZSBhbmQKd2lyaW5nIGl0IGludG8gZGlzY292ZXJfYWNjb3VudF9yZXNvdXJjZXMsIHNhbWUgcGF0dGVybiBhcyBBV1MncyBydW5u
ZXIucHkuCiIiIgppbXBvcnQganNvbgppbXBvcnQgbG9nZ2luZwoKZnJvbSBhenVyZS5pZGVudGl0eSBpbXBvcnQgQ2xpZW50U2Vj
cmV0Q3JlZGVudGlhbApmcm9tIGF6dXJlLm1nbXQuY29tcHV0ZSBpbXBvcnQgQ29tcHV0ZU1hbmFnZW1lbnRDbGllbnQKZnJvbSBh
enVyZS5tZ210LnN0b3JhZ2UgaW1wb3J0IFN0b3JhZ2VNYW5hZ2VtZW50Q2xpZW50CmZyb20gYXp1cmUubWdtdC5zcWwgaW1wb3J0
IFNxbE1hbmFnZW1lbnRDbGllbnQKZnJvbSBhenVyZS5tZ210LndlYiBpbXBvcnQgV2ViU2l0ZU1hbmFnZW1lbnRDbGllbnQKCmZy
b20gYXBwLmRiIGltcG9ydCBnZXRfY29ubmVjdGlvbgoKbG9nZ2VyID0gbG9nZ2luZy5nZXRMb2dnZXIoX19uYW1lX18pCgoKZGVm
IF9jcmVkZW50aWFsKGFjY291bnQ6IGRpY3QsIHNlY3JldDogc3RyKSAtPiBDbGllbnRTZWNyZXRDcmVkZW50aWFsOgogICAgcmV0
dXJuIENsaWVudFNlY3JldENyZWRlbnRpYWwoCiAgICAgICAgdGVuYW50X2lkPWFjY291bnRbInRlbmFudF9pZCJdLAogICAgICAg
IGNsaWVudF9pZD1hY2NvdW50WyJjbGllbnRfaWQiXSwKICAgICAgICBjbGllbnRfc2VjcmV0PXNlY3JldCwKICAgICkKCgpkZWYg
X3Vwc2VydF9yZXNvdXJjZShjdXJzb3IsIGFjY291bnRfaWQsIHJlc291cmNlX3R5cGUsIHJlc291cmNlX2lkLCBuYW1lLCB0YWdz
LCByZWdpb24sCiAgICAgICAgICAgICAgICAgICAgICBub3JtYWxpemVkX3Jlc291cmNlX3R5cGUpOgogICAgY3Vyc29yLmV4ZWN1
dGUoIiIiCiAgICAgICAgSU5TRVJUIElOVE8gcmVzb3VyY2VzCiAgICAgICAgICAgIChhd3NfYWNjb3VudF9pZCwgcmVzb3VyY2Vf
dHlwZSwgcmVzb3VyY2VfaWQsIG5hbWUsIHRhZ3MsIHJlZ2lvbiwgbm9ybWFsaXplZF9yZXNvdXJjZV90eXBlKQogICAgICAgIFZB
TFVFUyAoJXMsICVzLCAlcywgJXMsICVzLCAlcywgJXMpCiAgICAgICAgT04gRFVQTElDQVRFIEtFWSBVUERBVEUKICAgICAgICAg
ICAgbmFtZSA9IFZBTFVFUyhuYW1lKSwKICAgICAgICAgICAgdGFncyA9IFZBTFVFUyh0YWdzKSwKICAgICAgICAgICAgcmVnaW9u
ID0gVkFMVUVTKHJlZ2lvbiksCiAgICAgICAgICAgIG5vcm1hbGl6ZWRfcmVzb3VyY2VfdHlwZSA9IFZBTFVFUyhub3JtYWxpemVk
X3Jlc291cmNlX3R5cGUpCiAgICAiIiIsIChhY2NvdW50X2lkLCByZXNvdXJjZV90eXBlLCByZXNvdXJjZV9pZCwgbmFtZSwganNv
bi5kdW1wcyh0YWdzIG9yIHt9KSwgcmVnaW9uLAogICAgICAgICAgbm9ybWFsaXplZF9yZXNvdXJjZV90eXBlKSkKCgpkZWYgX2Rp
c2NvdmVyX3Ztcyhjb21wdXRlX2NsaWVudCwgYWNjb3VudF9pZCwgY3Vyc29yKSAtPiBpbnQ6CiAgICBjb3VudCA9IDAKICAgIGZv
ciB2bSBpbiBjb21wdXRlX2NsaWVudC52aXJ0dWFsX21hY2hpbmVzLmxpc3RfYWxsKCk6CiAgICAgICAgX3Vwc2VydF9yZXNvdXJj
ZSgKICAgICAgICAgICAgY3Vyc29yLCBhY2NvdW50X2lkLCAidm0iLCB2bS5pZCwgdm0ubmFtZSwKICAgICAgICAgICAgZGljdCh2
bS50YWdzIG9yIHt9KSwgdm0ubG9jYXRpb24sICJjb21wdXRlIiwKICAgICAgICApCiAgICAgICAgY291bnQgKz0gMQogICAgcmV0
dXJuIGNvdW50CgoKZGVmIF9kaXNjb3Zlcl9zdG9yYWdlX2FjY291bnRzKHN0b3JhZ2VfY2xpZW50LCBhY2NvdW50X2lkLCBjdXJz
b3IpIC0+IGludDoKICAgIGNvdW50ID0gMAogICAgZm9yIHNhIGluIHN0b3JhZ2VfY2xpZW50LnN0b3JhZ2VfYWNjb3VudHMubGlz
dCgpOgogICAgICAgIF91cHNlcnRfcmVzb3VyY2UoCiAgICAgICAgICAgIGN1cnNvciwgYWNjb3VudF9pZCwgInN0b3JhZ2VfYWNj
b3VudCIsIHNhLmlkLCBzYS5uYW1lLAogICAgICAgICAgICBkaWN0KHNhLnRhZ3Mgb3Ige30pLCBzYS5sb2NhdGlvbiwgInN0b3Jh
Z2UiLAogICAgICAgICkKICAgICAgICBjb3VudCArPSAxCiAgICByZXR1cm4gY291bnQKCgpkZWYgX2Rpc2NvdmVyX3NxbF9kYXRh
YmFzZXMoc3FsX2NsaWVudCwgYWNjb3VudF9pZCwgY3Vyc29yKSAtPiBpbnQ6CiAgICBjb3VudCA9IDAKICAgIGZvciBzZXJ2ZXIg
aW4gc3FsX2NsaWVudC5zZXJ2ZXJzLmxpc3QoKToKICAgICAgICAjIHNlcnZlci5pZCBsb29rcyBsaWtlIC4uLi9yZXNvdXJjZUdy
b3Vwcy97cmd9L3Byb3ZpZGVycy9NaWNyb3NvZnQuU3FsL3NlcnZlcnMve25hbWV9CiAgICAgICAgcmcgPSBzZXJ2ZXIuaWQuc3Bs
aXQoIi9yZXNvdXJjZUdyb3Vwcy8iKVsxXS5zcGxpdCgiLyIpWzBdCiAgICAgICAgZm9yIGRiIGluIHNxbF9jbGllbnQuZGF0YWJh
c2VzLmxpc3RfYnlfc2VydmVyKHJnLCBzZXJ2ZXIubmFtZSk6CiAgICAgICAgICAgIGlmIGRiLm5hbWUgPT0gIm1hc3RlciI6CiAg
ICAgICAgICAgICAgICBjb250aW51ZQogICAgICAgICAgICBfdXBzZXJ0X3Jlc291cmNlKAogICAgICAgICAgICAgICAgY3Vyc29y
LCBhY2NvdW50X2lkLCAic3FsX2RhdGFiYXNlIiwgZGIuaWQsIGYie3NlcnZlci5uYW1lfS97ZGIubmFtZX0iLAogICAgICAgICAg
ICAgICAgZGljdChkYi50YWdzIG9yIHt9KSwgZGIubG9jYXRpb24sICJkYXRhYmFzZSIsCiAgICAgICAgICAgICkKICAgICAgICAg
ICAgY291bnQgKz0gMQogICAgcmV0dXJuIGNvdW50CgoKZGVmIF9kaXNjb3Zlcl9hcHBfc2VydmljZXMod2ViX2NsaWVudCwgYWNj
b3VudF9pZCwgY3Vyc29yKSAtPiBpbnQ6CiAgICBjb3VudCA9IDAKICAgIGZvciBzaXRlIGluIHdlYl9jbGllbnQud2ViX2FwcHMu
bGlzdCgpOgogICAgICAgIF91cHNlcnRfcmVzb3VyY2UoCiAgICAgICAgICAgIGN1cnNvciwgYWNjb3VudF9pZCwgImFwcF9zZXJ2
aWNlIiwgc2l0ZS5pZCwgc2l0ZS5uYW1lLAogICAgICAgICAgICBkaWN0KHNpdGUudGFncyBvciB7fSksIHNpdGUubG9jYXRpb24s
ICJjb21wdXRlIiwKICAgICAgICApCiAgICAgICAgY291bnQgKz0gMQogICAgcmV0dXJuIGNvdW50CgoKZGVmIGRpc2NvdmVyX2Fj
Y291bnRfcmVzb3VyY2VzKGFjY291bnQ6IGRpY3QsIHNlY3JldDogc3RyKSAtPiBkaWN0OgogICAgIiIiUnVuIGRpc2NvdmVyeSBm
b3IgYSBzaW5nbGUgQXp1cmUgYWNjb3VudC4gUmV0dXJucyBhIHBlci10eXBlIGNvdW50LiIiIgogICAgY3JlZCA9IF9jcmVkZW50
aWFsKGFjY291bnQsIHNlY3JldCkKICAgIHN1Yl9pZCA9IGFjY291bnRbInN1YnNjcmlwdGlvbl9pZCJdCgogICAgY29tcHV0ZV9j
bGllbnQgPSBDb21wdXRlTWFuYWdlbWVudENsaWVudChjcmVkLCBzdWJfaWQpCiAgICBzdG9yYWdlX2NsaWVudCA9IFN0b3JhZ2VN
YW5hZ2VtZW50Q2xpZW50KGNyZWQsIHN1Yl9pZCkKICAgIHNxbF9jbGllbnQgPSBTcWxNYW5hZ2VtZW50Q2xpZW50KGNyZWQsIHN1
Yl9pZCkKICAgIHdlYl9jbGllbnQgPSBXZWJTaXRlTWFuYWdlbWVudENsaWVudChjcmVkLCBzdWJfaWQpCgogICAgY29ubiA9IGdl
dF9jb25uZWN0aW9uKCkKICAgIGN1cnNvciA9IGNvbm4uY3Vyc29yKCkKICAgIGNvdW50cyA9IHsidm0iOiAwLCAic3RvcmFnZV9h
Y2NvdW50IjogMCwgInNxbF9kYXRhYmFzZSI6IDAsICJhcHBfc2VydmljZSI6IDB9CiAgICB0cnk6CiAgICAgICAgY291bnRzWyJ2
bSJdID0gX2Rpc2NvdmVyX3Ztcyhjb21wdXRlX2NsaWVudCwgYWNjb3VudFsiaWQiXSwgY3Vyc29yKQogICAgICAgIGNvdW50c1si
c3RvcmFnZV9hY2NvdW50Il0gPSBfZGlzY292ZXJfc3RvcmFnZV9hY2NvdW50cyhzdG9yYWdlX2NsaWVudCwgYWNjb3VudFsiaWQi
XSwgY3Vyc29yKQogICAgICAgIGNvdW50c1sic3FsX2RhdGFiYXNlIl0gPSBfZGlzY292ZXJfc3FsX2RhdGFiYXNlcyhzcWxfY2xp
ZW50LCBhY2NvdW50WyJpZCJdLCBjdXJzb3IpCiAgICAgICAgY291bnRzWyJhcHBfc2VydmljZSJdID0gX2Rpc2NvdmVyX2FwcF9z
ZXJ2aWNlcyh3ZWJfY2xpZW50LCBhY2NvdW50WyJpZCJdLCBjdXJzb3IpCiAgICAgICAgY29ubi5jb21taXQoKQogICAgZXhjZXB0
IEV4Y2VwdGlvbjoKICAgICAgICBjb25uLnJvbGxiYWNrKCkKICAgICAgICByYWlzZQogICAgZmluYWxseToKICAgICAgICBjdXJz
b3IuY2xvc2UoKQogICAgICAgIGNvbm4uY2xvc2UoKQoKICAgIGxvZ2dlci5pbmZvKGYiQXp1cmUgZGlzY292ZXJ5IGZvciB7YWNj
b3VudC5nZXQoJ2FjY291bnRfbmFtZScpfToge2NvdW50c30iKQogICAgcmV0dXJuIGNvdW50cwo=
"""},
    'app/providers/azure/metric_catalog_data.py': {"new": False, "b64": """
IyBhcHAvcHJvdmlkZXJzL2F6dXJlL21ldHJpY19jYXRhbG9nX2RhdGEucHkKIiIiCkN1cmF0ZWQgQXp1cmUgTW9uaXRvciBwbGF0
Zm9ybS1tZXRyaWMgY2F0YWxvZywgc2FtZSBzaGFwZSBhcwphcHAuYXdzLm1ldHJpY19jYXRhbG9nX2RhdGEuQ1VSQVRFRDogc2Vy
dmljZSBrZXkgLT4gKGRpc3BsYXkgbmFtZSwKbmFtZXNwYWNlLCBjYXRlZ29yeSwgWyAobWV0cmljX25hbWUsIHVuaXQsIHN0YXRp
c3RpYywgaXNfZGVmYXVsdCwKZGVzY3JpcHRpb24pLCAuLi4gXSkuCgpgbmFtZXNwYWNlYCBoZXJlIGlzIHRoZSBBenVyZSBNb25p
dG9yIG1ldHJpYyBuYW1lc3BhY2UgKE1pY3Jvc29mdC4qCnJlc291cmNlLXByb3ZpZGVyIHR5cGUpLCB1c2VkIGJvdGggZm9yIGRp
c3BsYXkgYW5kIHRvIGdyb3VwIHJlc291cmNlcyBieQp0eXBlIGR1cmluZyBkaXNjb3ZlcnkvY29uc29sZS1saW5rIGRpc3BhdGNo
LiBNZXRyaWMgbmFtZXMvdW5pdHMgYXJlIHRoZQpyZWFsIEF6dXJlIE1vbml0b3IgcGxhdGZvcm0gbWV0cmljIG5hbWVzIOKAlCBz
ZWUKaHR0cHM6Ly9sZWFybi5taWNyb3NvZnQuY29tL2F6dXJlL2F6dXJlLW1vbml0b3IvcmVmZXJlbmNlL3N1cHBvcnRlZC1tZXRy
aWNzL21ldHJpY3MtaW5kZXgKIiIiCgpDVVJBVEVEID0gewogICAgInZtIjogKCJBenVyZSBWaXJ0dWFsIE1hY2hpbmVzIiwgIk1p
Y3Jvc29mdC5Db21wdXRlL3ZpcnR1YWxNYWNoaW5lcyIsICJjb3JlIiwgWwogICAgICAgICgiUGVyY2VudGFnZSBDUFUiLCAgICAg
ICAgICAiUGVyY2VudCIsICJBdmVyYWdlIiwgVHJ1ZSwgICIlIENQVSB1c2VkIiksCiAgICAgICAgKCJOZXR3b3JrIEluIFRvdGFs
IiwgICAgICAgICJCeXRlcyIsICAgIlRvdGFsIiwgICBUcnVlLCAgIkluYm91bmQgbmV0d29yayB0cmFmZmljIiksCiAgICAgICAg
KCJOZXR3b3JrIE91dCBUb3RhbCIsICAgICAgICJCeXRlcyIsICAgIlRvdGFsIiwgICBUcnVlLCAgIk91dGJvdW5kIG5ldHdvcmsg
dHJhZmZpYyIpLAogICAgICAgICgiRGlzayBSZWFkIEJ5dGVzIiwgICAgICAgICAiQnl0ZXMiLCAgICJUb3RhbCIsICAgRmFsc2Us
ICJEaXNrIHJlYWQgdGhyb3VnaHB1dCIpLAogICAgICAgICgiRGlzayBXcml0ZSBCeXRlcyIsICAgICAgICAiQnl0ZXMiLCAgICJU
b3RhbCIsICAgRmFsc2UsICJEaXNrIHdyaXRlIHRocm91Z2hwdXQiKSwKICAgICAgICAoIkRpc2sgUmVhZCBPcGVyYXRpb25zL1Nl
YyIsIkNvdW50UGVyU2Vjb25kIiwgIkF2ZXJhZ2UiLCBGYWxzZSwgIkRpc2sgcmVhZCBJT1BTIiksCiAgICAgICAgKCJEaXNrIFdy
aXRlIE9wZXJhdGlvbnMvU2VjIiwiQ291bnRQZXJTZWNvbmQiLCJBdmVyYWdlIiwgRmFsc2UsICJEaXNrIHdyaXRlIElPUFMiKSwK
ICAgICAgICAoIlZNIEF2YWlsYWJpbGl0eSBNZXRyaWMiLCAgIkNvdW50IiwgICAiQXZlcmFnZSIsIFRydWUsICAiVk0gdXB0aW1l
L2hlYWx0aCBzaWduYWwiKSwKICAgICAgICAoIkNQVSBDcmVkaXRzIFJlbWFpbmluZyIsICAgIkNvdW50IiwgICAiQXZlcmFnZSIs
IEZhbHNlLCAiQnVyc3RhYmxlIChCLXNlcmllcykgQ1BVIGNyZWRpdHMgYXZhaWxhYmxlIiksCiAgICBdKSwKICAgICJzdG9yYWdl
X2FjY291bnQiOiAoIkF6dXJlIFN0b3JhZ2UgQWNjb3VudHMiLCAiTWljcm9zb2Z0LlN0b3JhZ2Uvc3RvcmFnZUFjY291bnRzIiwg
ImNvcmUiLCBbCiAgICAgICAgKCJVc2VkQ2FwYWNpdHkiLCAgICAgICAgICJCeXRlcyIsICAgIkF2ZXJhZ2UiLCBUcnVlLCAgIlRv
dGFsIHN0b3JhZ2UgdXNlZCIpLAogICAgICAgICgiVHJhbnNhY3Rpb25zIiwgICAgICAgICAiQ291bnQiLCAgICJUb3RhbCIsICAg
VHJ1ZSwgICJUb3RhbCBBUEkgdHJhbnNhY3Rpb25zIiksCiAgICAgICAgKCJJbmdyZXNzIiwgICAgICAgICAgICAgICJCeXRlcyIs
ICAgIlRvdGFsIiwgICBGYWxzZSwgIkRhdGEgaW5ncmVzcyIpLAogICAgICAgICgiRWdyZXNzIiwgICAgICAgICAgICAgICAiQnl0
ZXMiLCAgICJUb3RhbCIsICAgRmFsc2UsICJEYXRhIGVncmVzcyIpLAogICAgICAgICgiU3VjY2Vzc1NlcnZlckxhdGVuY3kiLCAi
TWlsbGlTZWNvbmRzIiwgIkF2ZXJhZ2UiLCBUcnVlLCAiU2VydmVyLXNpZGUgbGF0ZW5jeSIpLAogICAgICAgICgiQXZhaWxhYmls
aXR5IiwgICAgICAgICAiUGVyY2VudCIsICJBdmVyYWdlIiwgVHJ1ZSwgICIlIHN1Y2Nlc3NmdWwgcmVxdWVzdHMiKSwKICAgIF0p
LAogICAgInNxbF9kYXRhYmFzZSI6ICgiQXp1cmUgU1FMIERhdGFiYXNlIiwgIk1pY3Jvc29mdC5TcWwvc2VydmVycy9kYXRhYmFz
ZXMiLCAiY29yZSIsIFsKICAgICAgICAoImNwdV9wZXJjZW50IiwgICAgICAgICAgICAgIlBlcmNlbnQiLCAiQXZlcmFnZSIsIFRy
dWUsICAiJSBDUFUgdXNlZCIpLAogICAgICAgICgiZHR1X2NvbnN1bXB0aW9uX3BlcmNlbnQiLCAiUGVyY2VudCIsICJBdmVyYWdl
IiwgVHJ1ZSwgICIlIERUVSBjb25zdW1lZCAoRFRVLWJhc2VkIHRpZXJzKSIpLAogICAgICAgICgic3RvcmFnZV9wZXJjZW50Iiwg
ICAgICAgICAiUGVyY2VudCIsICJBdmVyYWdlIiwgVHJ1ZSwgICIlIHN0b3JhZ2UgdXNlZCIpLAogICAgICAgICgiY29ubmVjdGlv
bl9zdWNjZXNzZnVsIiwgICAiQ291bnQiLCAgICJUb3RhbCIsICAgRmFsc2UsICJTdWNjZXNzZnVsIGNvbm5lY3Rpb25zIiksCiAg
ICAgICAgKCJjb25uZWN0aW9uX2ZhaWxlZCIsICAgICAgICJDb3VudCIsICAgIlRvdGFsIiwgICBUcnVlLCAgIkZhaWxlZCBjb25u
ZWN0aW9ucyIpLAogICAgICAgICgiZGVhZGxvY2siLCAgICAgICAgICAgICAgICAiQ291bnQiLCAgICJUb3RhbCIsICAgRmFsc2Us
ICJEZWFkbG9ja3MiKSwKICAgIF0pLAogICAgImFwcF9zZXJ2aWNlIjogKCJBenVyZSBBcHAgU2VydmljZSIsICJNaWNyb3NvZnQu
V2ViL3NpdGVzIiwgImNvcmUiLCBbCiAgICAgICAgKCJDcHVUaW1lIiwgICAgICAgICAgICAgIlNlY29uZHMiLCAiVG90YWwiLCAg
IEZhbHNlLCAiQ1BVIHRpbWUgY29uc3VtZWQiKSwKICAgICAgICAoIkh0dHA1eHgiLCAgICAgICAgICAgICAiQ291bnQiLCAgICJU
b3RhbCIsICAgVHJ1ZSwgICJTZXJ2ZXIgZXJyb3JzIiksCiAgICAgICAgKCJSZXF1ZXN0cyIsICAgICAgICAgICAgIkNvdW50Iiwg
ICAiVG90YWwiLCAgIFRydWUsICAiVG90YWwgcmVxdWVzdHMiKSwKICAgICAgICAoIkF2ZXJhZ2VSZXNwb25zZVRpbWUiLCAiU2Vj
b25kcyIsICJBdmVyYWdlIiwgVHJ1ZSwgICJBdmVyYWdlIHJlc3BvbnNlIHRpbWUiKSwKICAgICAgICAoIk1lbW9yeVdvcmtpbmdT
ZXQiLCAgICAiQnl0ZXMiLCAgICJBdmVyYWdlIiwgRmFsc2UsICJNZW1vcnkgaW4gdXNlIiksCiAgICBdKSwKfQoKIyBOYW1lc3Bh
Y2UgcmVnaXN0ZXJlZCBvbmx5IOKAlCBubyBoYW5kLWVudW1lcmF0ZWQgbWV0cmljIGxpc3QgKGZldGNoZWQgbGl2ZQojIHZpYSBB
enVyZSBNb25pdG9yJ3MgbWV0cmljLWRlZmluaXRpb25zIEFQSSBvbiBkZW1hbmQsIHNhbWUgcGF0dGVybiBhcyB0aGUKIyBBV1Mg
RElSRUNUT1JZIHRpZXIpLgpESVJFQ1RPUlkgPSBbCiAgICAoIkF6dXJlIExvYWQgQmFsYW5jZXIiLCAgICAgIk1pY3Jvc29mdC5O
ZXR3b3JrL2xvYWRCYWxhbmNlcnMiKSwKICAgICgiQXp1cmUgQXBwbGljYXRpb24gR2F0ZXdheSIsICJNaWNyb3NvZnQuTmV0d29y
ay9hcHBsaWNhdGlvbkdhdGV3YXlzIiksCiAgICAoIkF6dXJlIEtleSBWYXVsdCIsICAgICAgICAgIk1pY3Jvc29mdC5LZXlWYXVs
dC92YXVsdHMiKSwKICAgICgiQXp1cmUgQ29zbW9zIERCIiwgICAgICAgICAiTWljcm9zb2Z0LkRvY3VtZW50REIvZGF0YWJhc2VB
Y2NvdW50cyIpLAogICAgKCJBenVyZSBSZWRpcyBDYWNoZSIsICAgICAgICJNaWNyb3NvZnQuQ2FjaGUvUmVkaXMiKSwKICAgICgi
QXp1cmUgRnVuY3Rpb25zIiwgICAgICAgICAiTWljcm9zb2Z0LldlYi9zaXRlcy9mdW5jdGlvbnMiKSwKICAgICgiQXp1cmUgS3Vi
ZXJuZXRlcyBTZXJ2aWNlIiwiTWljcm9zb2Z0LkNvbnRhaW5lclNlcnZpY2UvbWFuYWdlZENsdXN0ZXJzIiksCl0K
"""},
    'app/providers/gcp/discovery.py': {"new": False, "b64": """
IyBhcHAvcHJvdmlkZXJzL2djcC9kaXNjb3ZlcnkucHkKIiIiClJlYWwgR0NQIHJlc291cmNlIGRpc2NvdmVyeS4gTWlycm9ycyBh
cHAuY29sbGVjdG9yLmRpc2NvdmVyeS5ydW5uZXIncwpjb250cmFjdDogd3JpdGVzL3VwZGF0ZXMgcm93cyBpbiBgcmVzb3VyY2Vz
YCwgc2NvcGVkIHRvIGEgc2luZ2xlIGFjY291bnQsCmNhbGxlZCBmcm9tIEdDUFByb3ZpZGVyLgoKQ29tcHV0ZSArIFN0b3JhZ2Ug
dXNlIHRoZSBnb29nbGUtY2xvdWQtKiBjbGllbnQgbGlicmFyaWVzOyBDbG91ZCBTUUwgdXNlcwp0aGUgU1FMIEFkbWluIEFQSSB2
aWEgZ29vZ2xlLWFwaS1weXRob24tY2xpZW50IChubyBkZWRpY2F0ZWQgZ29vZ2xlLWNsb3VkCmxpYnJhcnkgZXhpc3RzIGZvciBp
dCkuCiIiIgppbXBvcnQganNvbgppbXBvcnQgbG9nZ2luZwoKZnJvbSBnb29nbGUub2F1dGgyIGltcG9ydCBzZXJ2aWNlX2FjY291
bnQgYXMgZ2NwX3NlcnZpY2VfYWNjb3VudApmcm9tIGdvb2dsZS5jbG91ZCBpbXBvcnQgY29tcHV0ZV92MQpmcm9tIGdvb2dsZS5j
bG91ZCBpbXBvcnQgc3RvcmFnZSBhcyBnY3MKZnJvbSBnb29nbGUuY2xvdWQgaW1wb3J0IHJ1bl92Mgpmcm9tIGdvb2dsZWFwaWNs
aWVudC5kaXNjb3ZlcnkgaW1wb3J0IGJ1aWxkIGFzIGdhcGlfYnVpbGQKCmxvZ2dlciA9IGxvZ2dpbmcuZ2V0TG9nZ2VyKF9fbmFt
ZV9fKQoKU0NPUEVTID0gWyJodHRwczovL3d3dy5nb29nbGVhcGlzLmNvbS9hdXRoL2Nsb3VkLXBsYXRmb3JtLnJlYWQtb25seSJd
CgoKZGVmIF9jcmVkZW50aWFscyhzYV9rZXlfanNvbjogc3RyKToKICAgIGluZm8gPSBqc29uLmxvYWRzKHNhX2tleV9qc29uKQog
ICAgcmV0dXJuIGdjcF9zZXJ2aWNlX2FjY291bnQuQ3JlZGVudGlhbHMuZnJvbV9zZXJ2aWNlX2FjY291bnRfaW5mbyhpbmZvLCBz
Y29wZXM9U0NPUEVTKQoKCmRlZiBfdXBzZXJ0X3Jlc291cmNlKGN1cnNvciwgYWNjb3VudF9pZCwgcmVzb3VyY2VfdHlwZSwgcmVz
b3VyY2VfaWQsIG5hbWUsIHRhZ3MsIHJlZ2lvbiwKICAgICAgICAgICAgICAgICAgICAgIG5vcm1hbGl6ZWRfcmVzb3VyY2VfdHlw
ZSk6CiAgICBjdXJzb3IuZXhlY3V0ZSgiIiIKICAgICAgICBJTlNFUlQgSU5UTyByZXNvdXJjZXMKICAgICAgICAgICAgKGF3c19h
Y2NvdW50X2lkLCByZXNvdXJjZV90eXBlLCByZXNvdXJjZV9pZCwgbmFtZSwgdGFncywgcmVnaW9uLCBub3JtYWxpemVkX3Jlc291
cmNlX3R5cGUpCiAgICAgICAgVkFMVUVTICglcywgJXMsICVzLCAlcywgJXMsICVzLCAlcykKICAgICAgICBPTiBEVVBMSUNBVEUg
S0VZIFVQREFURQogICAgICAgICAgICBuYW1lID0gVkFMVUVTKG5hbWUpLAogICAgICAgICAgICB0YWdzID0gVkFMVUVTKHRhZ3Mp
LAogICAgICAgICAgICByZWdpb24gPSBWQUxVRVMocmVnaW9uKSwKICAgICAgICAgICAgbm9ybWFsaXplZF9yZXNvdXJjZV90eXBl
ID0gVkFMVUVTKG5vcm1hbGl6ZWRfcmVzb3VyY2VfdHlwZSkKICAgICIiIiwgKGFjY291bnRfaWQsIHJlc291cmNlX3R5cGUsIHJl
c291cmNlX2lkLCBuYW1lLCBqc29uLmR1bXBzKHRhZ3Mgb3Ige30pLCByZWdpb24sCiAgICAgICAgICBub3JtYWxpemVkX3Jlc291
cmNlX3R5cGUpKQoKCmRlZiBfZGlzY292ZXJfY29tcHV0ZV9pbnN0YW5jZXMoY3JlZHMsIHByb2plY3RfaWQsIGFjY291bnRfaWQs
IGN1cnNvcikgLT4gaW50OgogICAgY2xpZW50ID0gY29tcHV0ZV92MS5JbnN0YW5jZXNDbGllbnQoY3JlZGVudGlhbHM9Y3JlZHMp
CiAgICBjb3VudCA9IDAKICAgIGZvciB6b25lLCByZXNwb25zZSBpbiBjbGllbnQuYWdncmVnYXRlZF9saXN0KHByb2plY3Q9cHJv
amVjdF9pZCk6CiAgICAgICAgaWYgbm90IHJlc3BvbnNlLmluc3RhbmNlczoKICAgICAgICAgICAgY29udGludWUKICAgICAgICB6
b25lX25hbWUgPSB6b25lLnNwbGl0KCIvIilbLTFdCiAgICAgICAgZm9yIGluc3QgaW4gcmVzcG9uc2UuaW5zdGFuY2VzOgogICAg
ICAgICAgICBsYWJlbHMgPSBkaWN0KGluc3QubGFiZWxzIG9yIHt9KQogICAgICAgICAgICByZXNvdXJjZV9pZCA9IGYicHJvamVj
dHMve3Byb2plY3RfaWR9L3pvbmVzL3t6b25lX25hbWV9L2luc3RhbmNlcy97aW5zdC5uYW1lfSIKICAgICAgICAgICAgX3Vwc2Vy
dF9yZXNvdXJjZSgKICAgICAgICAgICAgICAgIGN1cnNvciwgYWNjb3VudF9pZCwgImNvbXB1dGVfaW5zdGFuY2UiLCByZXNvdXJj
ZV9pZCwgaW5zdC5uYW1lLAogICAgICAgICAgICAgICAgbGFiZWxzLCB6b25lX25hbWUsICJjb21wdXRlIiwKICAgICAgICAgICAg
KQogICAgICAgICAgICBjb3VudCArPSAxCiAgICByZXR1cm4gY291bnQKCgpkZWYgX2Rpc2NvdmVyX2djc19idWNrZXRzKGNyZWRz
LCBwcm9qZWN0X2lkLCBhY2NvdW50X2lkLCBjdXJzb3IpIC0+IGludDoKICAgIGNsaWVudCA9IGdjcy5DbGllbnQocHJvamVjdD1w
cm9qZWN0X2lkLCBjcmVkZW50aWFscz1jcmVkcykKICAgIGNvdW50ID0gMAogICAgZm9yIGJ1Y2tldCBpbiBjbGllbnQubGlzdF9i
dWNrZXRzKCk6CiAgICAgICAgcmVzb3VyY2VfaWQgPSBmInByb2plY3RzL3twcm9qZWN0X2lkfS9idWNrZXRzL3tidWNrZXQubmFt
ZX0iCiAgICAgICAgX3Vwc2VydF9yZXNvdXJjZSgKICAgICAgICAgICAgY3Vyc29yLCBhY2NvdW50X2lkLCAiZ2NzX2J1Y2tldCIs
IHJlc291cmNlX2lkLCBidWNrZXQubmFtZSwKICAgICAgICAgICAgZGljdChidWNrZXQubGFiZWxzIG9yIHt9KSwgYnVja2V0Lmxv
Y2F0aW9uIG9yICIiLCAic3RvcmFnZSIsCiAgICAgICAgKQogICAgICAgIGNvdW50ICs9IDEKICAgIHJldHVybiBjb3VudAoKCmRl
ZiBfZGlzY292ZXJfY2xvdWRzcWxfaW5zdGFuY2VzKGNyZWRzLCBwcm9qZWN0X2lkLCBhY2NvdW50X2lkLCBjdXJzb3IpIC0+IGlu
dDoKICAgIHNlcnZpY2UgPSBnYXBpX2J1aWxkKCJzcWxhZG1pbiIsICJ2MWJldGE0IiwgY3JlZGVudGlhbHM9Y3JlZHMsIGNhY2hl
X2Rpc2NvdmVyeT1GYWxzZSkKICAgIGNvdW50ID0gMAogICAgcmVxID0gc2VydmljZS5pbnN0YW5jZXMoKS5saXN0KHByb2plY3Q9
cHJvamVjdF9pZCkKICAgIHdoaWxlIHJlcSBpcyBub3QgTm9uZToKICAgICAgICByZXNwID0gcmVxLmV4ZWN1dGUoKQogICAgICAg
IGZvciBpbnN0IGluIHJlc3AuZ2V0KCJpdGVtcyIsIFtdKToKICAgICAgICAgICAgcmVzb3VyY2VfaWQgPSBmInByb2plY3RzL3tw
cm9qZWN0X2lkfS9pbnN0YW5jZXMve2luc3RbJ25hbWUnXX0iCiAgICAgICAgICAgIF91cHNlcnRfcmVzb3VyY2UoCiAgICAgICAg
ICAgICAgICBjdXJzb3IsIGFjY291bnRfaWQsICJjbG91ZHNxbF9pbnN0YW5jZSIsIHJlc291cmNlX2lkLCBpbnN0WyJuYW1lIl0s
CiAgICAgICAgICAgICAgICBkaWN0KGluc3QuZ2V0KCJzZXR0aW5ncyIsIHt9KS5nZXQoInVzZXJMYWJlbHMiLCB7fSkgb3Ige30p
LAogICAgICAgICAgICAgICAgaW5zdC5nZXQoInJlZ2lvbiIsICIiKSwgImRhdGFiYXNlIiwKICAgICAgICAgICAgKQogICAgICAg
ICAgICBjb3VudCArPSAxCiAgICAgICAgcmVxID0gc2VydmljZS5pbnN0YW5jZXMoKS5saXN0X25leHQocHJldmlvdXNfcmVxdWVz
dD1yZXEsIHByZXZpb3VzX3Jlc3BvbnNlPXJlc3ApCiAgICByZXR1cm4gY291bnQKCgojIENsb3VkIFJ1biBpcyByZWdpb25hbCB3
aXRoIG5vICJsaXN0IGFjcm9zcyBhbGwgcmVnaW9ucyIgY2FsbCwgc28gd2UgcHJvYmUKIyB0aGUgc2FtZSBzZXQgb2YgcmVnaW9u
cyBvZmZlcmVkIGluIHRoZSBvbmJvYXJkaW5nIFVJJ3MgR0NQIHJlZ2lvbiBwaWNrZXIuCiMgQSByZWdpb24gd2l0aCBubyBDbG91
ZCBSdW4gc2VydmljZXMgKG9yIHdoZXJlIHRoZSBBUEkgaXNuJ3QgZW5hYmxlZCkKIyByYWlzZXMgaGVyZSDigJQgY2F1Z2h0IGFu
ZCBza2lwcGVkIHBlci1yZWdpb24gcmF0aGVyIHRoYW4gZmFpbGluZyB0aGUKIyB3aG9sZSBkaXNjb3ZlcnkgcnVuLgpDTE9VRF9S
VU5fUkVHSU9OUyA9IFsKICAgICJhc2lhLXNvdXRoMSIsICJhc2lhLXNvdXRoMiIsICJhc2lhLXNvdXRoZWFzdDEiLCAiYXNpYS1l
YXN0MSIsICJhc2lhLW5vcnRoZWFzdDEiLAogICAgImF1c3RyYWxpYS1zb3V0aGVhc3QxIiwgInVzLWNlbnRyYWwxIiwgInVzLWVh
c3QxIiwgInVzLXdlc3QxIiwKICAgICJldXJvcGUtd2VzdDEiLCAiZXVyb3BlLXdlc3QyIiwgImV1cm9wZS1jZW50cmFsMiIsCl0K
CgpkZWYgX2Rpc2NvdmVyX2Nsb3VkX3J1bihjcmVkcywgcHJvamVjdF9pZCwgYWNjb3VudF9pZCwgY3Vyc29yKSAtPiBpbnQ6CiAg
ICBjbGllbnQgPSBydW5fdjIuU2VydmljZXNDbGllbnQoY3JlZGVudGlhbHM9Y3JlZHMpCiAgICBjb3VudCA9IDAKICAgIGZvciBy
ZWdpb24gaW4gQ0xPVURfUlVOX1JFR0lPTlM6CiAgICAgICAgcGFyZW50ID0gZiJwcm9qZWN0cy97cHJvamVjdF9pZH0vbG9jYXRp
b25zL3tyZWdpb259IgogICAgICAgIHRyeToKICAgICAgICAgICAgZm9yIHN2YyBpbiBjbGllbnQubGlzdF9zZXJ2aWNlcyhwYXJl
bnQ9cGFyZW50KToKICAgICAgICAgICAgICAgIG5hbWUgPSBzdmMubmFtZS5zcGxpdCgiLyIpWy0xXQogICAgICAgICAgICAgICAg
cmVzb3VyY2VfaWQgPSBzdmMubmFtZSAgIyBwcm9qZWN0cy97cH0vbG9jYXRpb25zL3tyfS9zZXJ2aWNlcy97bmFtZX0KICAgICAg
ICAgICAgICAgIF91cHNlcnRfcmVzb3VyY2UoCiAgICAgICAgICAgICAgICAgICAgY3Vyc29yLCBhY2NvdW50X2lkLCAiY2xvdWRf
cnVuX3NlcnZpY2UiLCByZXNvdXJjZV9pZCwgbmFtZSwKICAgICAgICAgICAgICAgICAgICBkaWN0KHN2Yy5sYWJlbHMgb3Ige30p
LCByZWdpb24sICJjb21wdXRlIiwKICAgICAgICAgICAgICAgICkKICAgICAgICAgICAgICAgIGNvdW50ICs9IDEKICAgICAgICBl
eGNlcHQgRXhjZXB0aW9uIGFzIGU6CiAgICAgICAgICAgIGxvZ2dlci5kZWJ1ZyhmIkNsb3VkIFJ1biBkaXNjb3Zlcnkgc2tpcHBl
ZCBmb3Ige3Byb2plY3RfaWR9L3tyZWdpb259OiB7ZX0iKQogICAgcmV0dXJuIGNvdW50CgoKZGVmIGRpc2NvdmVyX2FjY291bnRf
cmVzb3VyY2VzKGFjY291bnQ6IGRpY3QsIHNhX2tleV9qc29uOiBzdHIpIC0+IGRpY3Q6CiAgICAiIiJSdW4gZGlzY292ZXJ5IGZv
ciBhIHNpbmdsZSBHQ1AgYWNjb3VudC4gUmV0dXJucyBhIHBlci10eXBlIGNvdW50LiIiIgogICAgY3JlZHMgPSBfY3JlZGVudGlh
bHMoc2Ffa2V5X2pzb24pCiAgICBwcm9qZWN0X2lkID0gYWNjb3VudFsicHJvamVjdF9pZCJdCgogICAgZnJvbSBhcHAuZGIgaW1w
b3J0IGdldF9jb25uZWN0aW9uCiAgICBjb25uID0gZ2V0X2Nvbm5lY3Rpb24oKQogICAgY3Vyc29yID0gY29ubi5jdXJzb3IoKQog
ICAgY291bnRzID0geyJjb21wdXRlX2luc3RhbmNlIjogMCwgImdjc19idWNrZXQiOiAwLCAiY2xvdWRzcWxfaW5zdGFuY2UiOiAw
LCAiY2xvdWRfcnVuX3NlcnZpY2UiOiAwfQogICAgdHJ5OgogICAgICAgIGNvdW50c1siY29tcHV0ZV9pbnN0YW5jZSJdID0gX2Rp
c2NvdmVyX2NvbXB1dGVfaW5zdGFuY2VzKGNyZWRzLCBwcm9qZWN0X2lkLCBhY2NvdW50WyJpZCJdLCBjdXJzb3IpCiAgICAgICAg
Y291bnRzWyJnY3NfYnVja2V0Il0gPSBfZGlzY292ZXJfZ2NzX2J1Y2tldHMoY3JlZHMsIHByb2plY3RfaWQsIGFjY291bnRbImlk
Il0sIGN1cnNvcikKICAgICAgICBjb3VudHNbImNsb3Vkc3FsX2luc3RhbmNlIl0gPSBfZGlzY292ZXJfY2xvdWRzcWxfaW5zdGFu
Y2VzKGNyZWRzLCBwcm9qZWN0X2lkLCBhY2NvdW50WyJpZCJdLCBjdXJzb3IpCiAgICAgICAgY291bnRzWyJjbG91ZF9ydW5fc2Vy
dmljZSJdID0gX2Rpc2NvdmVyX2Nsb3VkX3J1bihjcmVkcywgcHJvamVjdF9pZCwgYWNjb3VudFsiaWQiXSwgY3Vyc29yKQogICAg
ICAgIGNvbm4uY29tbWl0KCkKICAgIGV4Y2VwdCBFeGNlcHRpb246CiAgICAgICAgY29ubi5yb2xsYmFjaygpCiAgICAgICAgcmFp
c2UKICAgIGZpbmFsbHk6CiAgICAgICAgY3Vyc29yLmNsb3NlKCkKICAgICAgICBjb25uLmNsb3NlKCkKCiAgICBsb2dnZXIuaW5m
byhmIkdDUCBkaXNjb3ZlcnkgZm9yIHthY2NvdW50LmdldCgnYWNjb3VudF9uYW1lJyl9OiB7Y291bnRzfSIpCiAgICByZXR1cm4g
Y291bnRzCg==
"""},
    'app/providers/gcp/metric_catalog_data.py': {"new": False, "b64": """
IyBhcHAvcHJvdmlkZXJzL2djcC9tZXRyaWNfY2F0YWxvZ19kYXRhLnB5CiIiIgpDdXJhdGVkIEdDUCBDbG91ZCBNb25pdG9yaW5n
IG1ldHJpYyBjYXRhbG9nLCBzYW1lIHNoYXBlIGFzCmFwcC5hd3MubWV0cmljX2NhdGFsb2dfZGF0YS5DVVJBVEVELiBgbmFtZXNw
YWNlYCBoZXJlIGlzIHRoZSBDbG91ZApNb25pdG9yaW5nIG1ldHJpYy1wcmVmaXggKGNvbXB1dGUuZ29vZ2xlYXBpcy5jb20vLi4u
LCBldGMpIOKAlCBzZWUKaHR0cHM6Ly9jbG91ZC5nb29nbGUuY29tL21vbml0b3JpbmcvYXBpL21ldHJpY3NfZ2NwCiIiIgoKQ1VS
QVRFRCA9IHsKICAgICJjb21wdXRlX2luc3RhbmNlIjogKCJDb21wdXRlIEVuZ2luZSIsICJjb21wdXRlLmdvb2dsZWFwaXMuY29t
L2luc3RhbmNlIiwgImNvcmUiLCBbCiAgICAgICAgKCJjcHUvdXRpbGl6YXRpb24iLCAgICAgICAgICAgICAgICAgICJQZXJjZW50
IiwgICJBdmVyYWdlIiwgVHJ1ZSwgICIlIENQVSB1c2VkIiksCiAgICAgICAgKCJuZXR3b3JrL3JlY2VpdmVkX2J5dGVzX2NvdW50
IiwgICAgICJCeXRlcyIsICAgICJUb3RhbCIsICAgVHJ1ZSwgICJJbmJvdW5kIG5ldHdvcmsgdHJhZmZpYyIpLAogICAgICAgICgi
bmV0d29yay9zZW50X2J5dGVzX2NvdW50IiwgICAgICAgICAiQnl0ZXMiLCAgICAiVG90YWwiLCAgIFRydWUsICAiT3V0Ym91bmQg
bmV0d29yayB0cmFmZmljIiksCiAgICAgICAgKCJkaXNrL3JlYWRfYnl0ZXNfY291bnQiLCAgICAgICAgICAgICJCeXRlcyIsICAg
ICJUb3RhbCIsICAgRmFsc2UsICJEaXNrIHJlYWQgdGhyb3VnaHB1dCIpLAogICAgICAgICgiZGlzay93cml0ZV9ieXRlc19jb3Vu
dCIsICAgICAgICAgICAiQnl0ZXMiLCAgICAiVG90YWwiLCAgIEZhbHNlLCAiRGlzayB3cml0ZSB0aHJvdWdocHV0IiksCiAgICAg
ICAgKCJkaXNrL3JlYWRfb3BzX2NvdW50IiwgICAgICAgICAgICAgICJDb3VudCIsICAgICJUb3RhbCIsICAgRmFsc2UsICJEaXNr
IHJlYWQgSU9QUyIpLAogICAgICAgICgiZGlzay93cml0ZV9vcHNfY291bnQiLCAgICAgICAgICAgICAiQ291bnQiLCAgICAiVG90
YWwiLCAgIEZhbHNlLCAiRGlzayB3cml0ZSBJT1BTIiksCiAgICAgICAgKCJ1cHRpbWUiLCAgICAgICAgICAgICAgICAgICAgICAg
ICAgICJTZWNvbmRzIiwgICJUb3RhbCIsICAgVHJ1ZSwgICJJbnN0YW5jZSB1cHRpbWUiKSwKICAgIF0pLAogICAgImdjc19idWNr
ZXQiOiAoIkNsb3VkIFN0b3JhZ2UiLCAic3RvcmFnZS5nb29nbGVhcGlzLmNvbS9zdG9yYWdlIiwgImNvcmUiLCBbCiAgICAgICAg
KCJ0b3RhbF9ieXRlcyIsICAgICAgICAgICAiQnl0ZXMiLCAiQXZlcmFnZSIsIFRydWUsICAiVG90YWwgYnl0ZXMgc3RvcmVkIiks
CiAgICAgICAgKCJvYmplY3RfY291bnQiLCAgICAgICAgICAiQ291bnQiLCAiQXZlcmFnZSIsIFRydWUsICAiTnVtYmVyIG9mIG9i
amVjdHMiKSwKICAgICAgICAoImFwaS9yZXF1ZXN0X2NvdW50IiwgICAgICJDb3VudCIsICJUb3RhbCIsICAgVHJ1ZSwgICJUb3Rh
bCBBUEkgcmVxdWVzdHMiKSwKICAgIF0pLAogICAgImNsb3Vkc3FsX2luc3RhbmNlIjogKCJDbG91ZCBTUUwiLCAiY2xvdWRzcWwu
Z29vZ2xlYXBpcy5jb20vZGF0YWJhc2UiLCAiY29yZSIsIFsKICAgICAgICAoImNwdS91dGlsaXphdGlvbiIsICAgICAgICAgICAi
UGVyY2VudCIsICJBdmVyYWdlIiwgVHJ1ZSwgICIlIENQVSB1c2VkIiksCiAgICAgICAgKCJtZW1vcnkvdXRpbGl6YXRpb24iLCAg
ICAgICAgIlBlcmNlbnQiLCAiQXZlcmFnZSIsIFRydWUsICAiJSBtZW1vcnkgdXNlZCIpLAogICAgICAgICgiZGlzay91dGlsaXph
dGlvbiIsICAgICAgICAgICJQZXJjZW50IiwgIkF2ZXJhZ2UiLCBUcnVlLCAgIiUgc3RvcmFnZSB1c2VkIiksCiAgICAgICAgKCJu
ZXR3b3JrL2Nvbm5lY3Rpb25zIiwgICAgICAgIkNvdW50IiwgICAiQXZlcmFnZSIsIFRydWUsICAiQWN0aXZlIGNvbm5lY3Rpb25z
IiksCiAgICAgICAgKCJteXNxbC9yZXBsaWNhdGlvbi9zZWNvbmRzX2JlaGluZF9tYXN0ZXIiLCAiU2Vjb25kcyIsICJBdmVyYWdl
IiwgRmFsc2UsICJSZXBsaWNhIGxhZyIpLAogICAgXSksCiAgICAiY2xvdWRfcnVuX3NlcnZpY2UiOiAoIkNsb3VkIFJ1biIsICJy
dW4uZ29vZ2xlYXBpcy5jb20vY29udGFpbmVyIiwgImNvcmUiLCBbCiAgICAgICAgKCJjcHUvdXRpbGl6YXRpb25zIiwgICAgICAg
ICAgICJQZXJjZW50IiwgIkF2ZXJhZ2UiLCBUcnVlLCAgIiUgQ1BVIHVzZWQgKHBlciBjb250YWluZXIgaW5zdGFuY2UpIiksCiAg
ICAgICAgKCJtZW1vcnkvdXRpbGl6YXRpb25zIiwgICAgICAgICJQZXJjZW50IiwgIkF2ZXJhZ2UiLCBUcnVlLCAgIiUgbWVtb3J5
IHVzZWQgKHBlciBjb250YWluZXIgaW5zdGFuY2UpIiksCiAgICAgICAgKCJyZXF1ZXN0X2NvdW50IiwgICAgICAgICAgICAgICJD
b3VudCIsICAgIlRvdGFsIiwgICBUcnVlLCAgIlRvdGFsIHJlcXVlc3RzIHNlcnZlZCIpLAogICAgICAgICgicmVxdWVzdF9sYXRl
bmNpZXMiLCAgICAgICAgICAiTWlsbGlTZWNvbmRzIiwgIkF2ZXJhZ2UiLCBUcnVlLCAiUmVxdWVzdCBsYXRlbmN5IiksCiAgICAg
ICAgKCJpbnN0YW5jZV9jb3VudCIsICAgICAgICAgICAgICJDb3VudCIsICAgIkF2ZXJhZ2UiLCBUcnVlLCAgIkFjdGl2ZSBjb250
YWluZXIgaW5zdGFuY2VzIiksCiAgICAgICAgKCJiaWxsYWJsZV9pbnN0YW5jZV90aW1lIiwgICAgICJTZWNvbmRzIiwgIlRvdGFs
IiwgICBGYWxzZSwgIkJpbGxhYmxlIGluc3RhbmNlLXRpbWUiKSwKICAgIF0pLAp9CgpESVJFQ1RPUlkgPSBbCiAgICAoIkNsb3Vk
IEZ1bmN0aW9ucyIsICAgICAgICJjbG91ZGZ1bmN0aW9ucy5nb29nbGVhcGlzLmNvbSIpLAogICAgKCJHb29nbGUgS3ViZXJuZXRl
cyBFbmdpbmUiLCAia3ViZXJuZXRlcy5pbyIpLAogICAgKCJDbG91ZCBMb2FkIEJhbGFuY2luZyIsICAibG9hZGJhbGFuY2luZy5n
b29nbGVhcGlzLmNvbSIpLAogICAgKCJQdWIvU3ViIiwgICAgICAgICAgICAgICAicHVic3ViLmdvb2dsZWFwaXMuY29tIiksCiAg
ICAoIkJpZ1F1ZXJ5IiwgICAgICAgICAgICAgICJiaWdxdWVyeS5nb29nbGVhcGlzLmNvbSIpLApdCg==
"""},
    'requirements.txt': {"new": False, "b64": """
YW5ub3RhdGVkLWRvYz09MC4wLjQKYW5ub3RhdGVkLXR5cGVzPT0wLjcuMAphbnlpbz09NC4xMi4xCmJvdG8zPT0xLjQyLjU0CmJv
dG9jb3JlPT0xLjQyLjU0CmNsaWNrPT04LjMuMQpjb2xvcmFtYT09MC40LjYKZmFzdGFwaT09MC4xMjkuMApoMTE9PTAuMTYuMApp
ZG5hPT0zLjExCmptZXNwYXRoPT0xLjEuMApteXNxbC1jb25uZWN0b3ItcHl0aG9uPT05LjYuMApweWRhbnRpYz09Mi4xMi41CnB5
ZGFudGljX2NvcmU9PTIuNDEuNQpweXRob24tZGF0ZXV0aWw9PTIuOS4wLnBvc3QwClB5WUFNTD09Ni4wLjMKczN0cmFuc2Zlcj09
MC4xNi4wCnNpeD09MS4xNy4wCnN0YXJsZXR0ZT09MC41Mi4xCnR5cGluZy1pbnNwZWN0aW9uPT0wLjQuMgp0eXBpbmdfZXh0ZW5z
aW9ucz09NC4xNS4wCnVybGxpYjM9PTIuNi4zCnV2aWNvcm49PTAuNDEuMAp1dmljb3JuW3N0YW5kYXJkXQp3ZWJzb2NrZXRzCnJl
ZGlzCmJjcnlwdApwYXNzbGliW2JjcnlwdF0KcmVxdWVzdHM9PTIuMzIuMwpweXRob24tZG90ZW52PT0xLjAuMQoKIyBBenVyZSBw
cm92aWRlciAoU2VydmljZSBQcmluY2lwYWwgYXV0aCArIHJlYWwgQVJNIGRpc2NvdmVyeSkKYXp1cmUtaWRlbnRpdHk+PTEuMTks
PDIKYXp1cmUtbWdtdC1yZXNvdXJjZT49MjMsPDI3CmF6dXJlLW1nbXQtY29tcHV0ZT49MzMsPDM4CmF6dXJlLW1nbXQtc3RvcmFn
ZT49MjEsPDIzCmF6dXJlLW1nbXQtc3FsPj00LDw1CmF6dXJlLW1nbXQtd2ViPj03LDw4CgojIEdDUCBwcm92aWRlciAoU2Vydmlj
ZSBBY2NvdW50IGF1dGggKyByZWFsIGRpc2NvdmVyeSkKZ29vZ2xlLWNsb3VkLWNvbXB1dGU+PTEuMTksPDIKZ29vZ2xlLWNsb3Vk
LXN0b3JhZ2U+PTIuMTgsPDMKZ29vZ2xlLWNsb3VkLXJlc291cmNlLW1hbmFnZXI+PTEuMTIsPDIKZ29vZ2xlLWNsb3VkLXJ1bj49
MC4xMCw8MQpnb29nbGUtYXBpLXB5dGhvbi1jbGllbnQ+PTIuMTQwLDwzCgojIENyZWRlbnRpYWwgZW5jcnlwdGlvbi1hdC1yZXN0
IChBenVyZSBjbGllbnQgc2VjcmV0IC8gR0NQIFNBIGtleSBKU09OKQpjcnlwdG9ncmFwaHk+PTQyLDw0NAo=
"""},
}

BACKUP_SUFFIX = ".bak.pre-region-fix-and-discovery-expansion"


def write_file(root: Path, relpath: str, content: bytes, dry_run: bool) -> str:
    target = root / relpath
    if target.exists():
        action = "PATCH"
        if not dry_run:
            shutil.copy2(target, str(target) + BACKUP_SUFFIX)
    else:
        action = "CREATE"
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
    if not dry_run:
        target.write_bytes(content)
    return action


def move_misplaced_009(root: Path, dry_run: bool) -> str:
    """009_multi_cloud_provider_columns.sql (+ its rollback) ended up at the
    project root instead of db/migrations/ — a casualty of the corrupted
    .gitignore fixed earlier. Move both into db/migrations/ if found at root."""
    moved = []
    for fname in ["009_multi_cloud_provider_columns.sql", "009_multi_cloud_provider_columns_rollback.sql"]:
        src = root / fname
        dst = root / "db" / "migrations" / fname
        if src.exists() and not dst.exists():
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dst))
            moved.append(fname)
        elif src.exists() and dst.exists():
            if not dry_run:
                src.unlink()
            moved.append(f"{fname} (duplicate at root removed)")
    if not moved:
        return "SKIPPED (not found at root — already tidy)"
    return "MOVED: " + ", ".join(moved)


def reword_service_detail_comment(root: Path, dry_run: bool) -> str:
    """The only remaining '📊' anywhere in the four originally-flagged pages
    is inside a source comment in ServiceDetail.jsx (never rendered) —
    reworded here to drop the emoji entirely, matching the rest of the
    already-converted codebase's comment style."""
    path = root / "frontend" / "src" / "pages" / "ServiceDetail.jsx"
    if not path.exists():
        return "SKIPPED (file not found)"
    text = path.read_text(encoding="utf-8")
    old = '// Deep-link support: if we arrived via Alerts\' "📊 Metrics" link'
    new = '// Deep-link support: if we arrived via Alerts\' "Metrics" link'
    if old not in text:
        return "SKIPPED (comment text not found — may already be edited, or emoji removed already)"
    if not dry_run:
        shutil.copy2(path, str(path) + BACKUP_SUFFIX)
        path.write_text(text.replace(old, new), encoding="utf-8")
    return "REWORDED (cosmetic — this text was never rendered to users)"


# ── DB migration application ────────────────────────────────────────────

MIGRATION_002_STATEMENTS = [
    "ALTER TABLE resources ADD COLUMN IF NOT EXISTS region VARCHAR(50) DEFAULT NULL AFTER tags",
    "ALTER TABLE resources ADD COLUMN IF NOT EXISTS instance_state VARCHAR(30) DEFAULT NULL AFTER region",
    "ALTER TABLE resources ADD INDEX IF NOT EXISTS idx_resources_region (aws_account_id, region)",
]

# (table, column, DDL) — same columns 009_multi_cloud_provider_columns.sql
# adds, re-applied here idempotently via information_schema since it's
# unclear whether that migration was ever actually run against this DB,
# and the Azure/GCP onboarding code on main depends on all of them.
MIGRATION_009_COLUMNS = [
    ("aws_accounts", "provider",
     "ALTER TABLE aws_accounts ADD COLUMN provider ENUM('aws','azure','gcp') NOT NULL DEFAULT 'aws' AFTER id"),
    ("aws_accounts", "tenant_id",
     "ALTER TABLE aws_accounts ADD COLUMN tenant_id VARCHAR(100) DEFAULT NULL AFTER external_id"),
    ("aws_accounts", "subscription_id",
     "ALTER TABLE aws_accounts ADD COLUMN subscription_id VARCHAR(100) DEFAULT NULL AFTER tenant_id"),
    ("aws_accounts", "client_id",
     "ALTER TABLE aws_accounts ADD COLUMN client_id VARCHAR(100) DEFAULT NULL AFTER subscription_id"),
    ("aws_accounts", "project_id",
     "ALTER TABLE aws_accounts ADD COLUMN project_id VARCHAR(100) DEFAULT NULL AFTER client_id"),
    ("aws_accounts", "service_account_email",
     "ALTER TABLE aws_accounts ADD COLUMN service_account_email VARCHAR(255) DEFAULT NULL AFTER project_id"),
    ("aws_accounts", "credential_ref",
     "ALTER TABLE aws_accounts ADD COLUMN credential_ref VARCHAR(255) DEFAULT NULL AFTER service_account_email"),
    ("resources", "normalized_resource_type",
     "ALTER TABLE resources ADD COLUMN normalized_resource_type VARCHAR(50) DEFAULT NULL AFTER resource_type"),
    ("metric_catalog", "provider",
     "ALTER TABLE metric_catalog ADD COLUMN provider ENUM('aws','azure','gcp') NOT NULL DEFAULT 'aws' AFTER service"),
]


def _column_exists(cursor, db_name, table, column) -> bool:
    cursor.execute("""
        SELECT COUNT(*) FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s
    """, (db_name, table, column))
    return cursor.fetchone()[0] > 0


def apply_migrations(root: Path, dry_run: bool) -> str:
    if dry_run:
        return "DRY-RUN (would connect to MySQL and apply migration 002 + re-check migration 009's columns)"
    try:
        import mysql.connector
    except ImportError:
        return "SKIPPED — mysql-connector-python not installed yet. Run `pip install -r requirements.txt` then re-run this script, or apply db/migrations/002_resources_region_instance_state.sql manually."

    try:
        from dotenv import load_dotenv
        load_dotenv(root / ".env")
    except ImportError:
        pass

    cfg = dict(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", 3306)),
        user=os.getenv("DB_USER", "monitor"),
        password=os.getenv("DB_PASSWORD", "root123"),
        database=os.getenv("DB_NAME", "monitoring_hub"),
    )
    try:
        conn = mysql.connector.connect(**cfg)
        cur = conn.cursor()

        for stmt in MIGRATION_002_STATEMENTS:
            cur.execute(stmt)
        conn.commit()
        lines = ["002_resources_region_instance_state.sql: APPLIED"]

        applied, already = [], []
        for table, column, ddl in MIGRATION_009_COLUMNS:
            if _column_exists(cur, cfg["database"], table, column):
                already.append(f"{table}.{column}")
            else:
                cur.execute(ddl)
                applied.append(f"{table}.{column}")
        conn.commit()
        if applied:
            lines.append(f"009_multi_cloud_provider_columns.sql: APPLIED missing column(s): {', '.join(applied)}")
        if already:
            lines.append(f"009_multi_cloud_provider_columns.sql: already present: {', '.join(already)}")

        cur.close()
        conn.close()
        return "\n  ".join(lines)
    except Exception as e:
        return (f"FAILED — {e}\n"
                f"  Apply manually:\n"
                f"    mysql -u{cfg['user']} -p {cfg['database']} < db/migrations/002_resources_region_instance_state.sql\n"
                f"    mysql -u{cfg['user']} -p {cfg['database']} < db/migrations/009_multi_cloud_provider_columns.sql")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path.cwd()
    if not (root / "app").exists() or not (root / "frontend").exists():
        print("ERROR: run this from the project root (folder containing app/ and frontend/)")
        sys.exit(1)

    print(f"{'DRY RUN — ' if args.dry_run else ''}Applying region-bug fix + discovery expansion in {root}\n")

    print("── Files ──")
    for relpath, meta in FILES.items():
        content = base64.b64decode(meta["b64"])
        action = write_file(root, relpath, content, args.dry_run)
        print(f"  [{action:6}] {relpath}  ({len(content)} bytes)")

    print("\n── Misplaced migration 009 cleanup ──")
    print(f"  {move_misplaced_009(root, args.dry_run)}")

    print("\n── ServiceDetail.jsx comment cleanup ──")
    print(f"  {reword_service_detail_comment(root, args.dry_run)}")

    print("\n── DB migrations (002 + re-check 009) ──")
    print(f"  {apply_migrations(root, args.dry_run)}")

    print("\n── emoji-to-icons-update/ staging folder ──")
    stale = root / "emoji-to-icons-update"
    if stale.exists():
        print(f"  FOUND at {stale} — this predates the console-url consolidation refactor.")
        print("  Do NOT merge it (would regress ServiceDetail.jsx). Safe to delete:")
        print(f"    rmdir /s /q emoji-to-icons-update   (PowerShell: Remove-Item -Recurse -Force emoji-to-icons-update)")
    else:
        print("  Not present — nothing to do.")

    if args.dry_run:
        print("\nDry run complete — nothing was written.")
    else:
        print("""
Done.

Next steps:
  1. pip install -r requirements.txt        (adds azure-mgmt-web, google-cloud-run)
  2. Restart uvicorn
  3. Trigger a discovery run (or wait for the next scheduled one) — the
     "Unknown column 'r.region'" error should be gone, and Azure/GCP
     accounts will now also pick up App Service / Cloud Run resources.

Backups of every overwritten file are alongside the originals, suffixed:
  .bak.pre-region-fix-and-discovery-expansion
""")


if __name__ == "__main__":
    main()
