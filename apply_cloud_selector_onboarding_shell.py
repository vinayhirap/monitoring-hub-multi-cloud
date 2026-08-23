#!/usr/bin/env python3
"""
apply_cloud_selector_onboarding_shell.py

Step 4 of the multi-cloud refactor plan (multi-cloud-architecture-
assessment.md, section 6, item 4): frontend cloud selector + provider-
aware onboarding shell.

WHAT THIS CHANGES
    frontend/src/pages/AccountOnboarding.jsx
        - Adds a provider tab bar (AWS / Azure / GCP) above the form.
        - AWS tab: existing form, byte-for-byte unchanged, just wrapped
          in a `{provider === "aws" && (...)}` conditional.
        - Azure/GCP tabs: new <ComingSoon /> placeholder — no backend
          call, no new fields, nothing submitted.
        - Onboarding queue sidebar stays visible regardless of tab.

WHAT THIS DOES NOT DO
    No Azure/GCP form fields, no backend routes, no DB writes for
    non-AWS providers. This is UX skeleton only, per the plan's
    explicit sequencing ("lets the UX skeleton land without gating on
    full Azure/GCP backend readiness").

SAFETY
    Anchor occurrence-count guarded, same as prior patch scripts.
    Backup: AccountOnboarding.jsx.bak.pre-cloud-selector-shell.

Run from the project root:
    python apply_cloud_selector_onboarding_shell.py --dry-run
    python apply_cloud_selector_onboarding_shell.py
"""

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path.cwd()
TARGET = ROOT / "frontend/src/pages/AccountOnboarding.jsx"
CSS_TARGET = ROOT / "frontend/src/pages/AccountOnboarding.css"
BACKUP_SUFFIX = ".bak.pre-cloud-selector-shell"

CSS_MARKER = "/* provider tabs — cloud selector shell */"
CSS_BLOCK = f'''

{CSS_MARKER}
.ob-provider-tabs {{
  display: flex;
  gap: 4px;
  margin-bottom: 16px;
  border-bottom: 1px solid rgba(99,130,190,0.15);
}}
.ob-provider-tab {{
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  color: rgba(99,130,190,0.65);
  font-size: 13px;
  font-weight: 600;
  padding: 8px 16px;
  cursor: pointer;
}}
.ob-provider-tab-active {{
  color: #e2e8f0;
  border-bottom-color: #2bb3ac;
}}
'''


def backup(path: Path):
    dest = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    shutil.copy2(path, dest)
    print(f"  backed up {path.name} -> {dest.name}")


def guarded_replace(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        print(f"  ABORT: anchor '{label}' found {count} times, expected 1.")
        print("  Local file has diverged from what this script expects — no changes written.")
        sys.exit(1)
    return text.replace(old, new)


# ── Edit 1: add PROVIDER_TABS const, right after ENVIRONMENTS ──

OLD_1 = '''const ENVIRONMENTS = ["Production", "Staging", "Development", "QA"];'''

NEW_1 = '''const ENVIRONMENTS = ["Production", "Staging", "Development", "QA"];

const PROVIDER_TABS = [
  { id: "aws",   label: "AWS" },
  { id: "azure", label: "Azure" },
  { id: "gcp",   label: "GCP" },
];'''

# ── Edit 2: add ComingSoon component, right after Field() ──

OLD_2 = '''function Field({ id, label, required, error, children }) {
  return (
    <div className={`ob-field ${error ? "ob-field-err" : ""}`}>
      <label htmlFor={id}>
        {label}{required && <span className="ob-req"> *</span>}
      </label>
      {children}
      {error && <span className="ob-err-msg">{error}</span>}
    </div>
  );
}'''

NEW_2 = '''function Field({ id, label, required, error, children }) {
  return (
    <div className={`ob-field ${error ? "ob-field-err" : ""}`}>
      <label htmlFor={id}>
        {label}{required && <span className="ob-req"> *</span>}
      </label>
      {children}
      {error && <span className="ob-err-msg">{error}</span>}
    </div>
  );
}

function ComingSoon({ provider }) {
  const label = provider === "azure" ? "Azure" : "GCP";
  return (
    <div className="ob-section" style={{ textAlign: "center", padding: "48px 24px" }}>
      <div className="ob-section-title">{label.toUpperCase()} — COMING SOON</div>
      <p className="ob-metrics-hint" style={{ marginTop: 12 }}>
        {label} account onboarding isn't available yet. AWS accounts can be
        onboarded today from the AWS tab.
      </p>
    </div>
  );
}'''

# ── Edit 3: add provider state, right after other useState in the component ──

OLD_3 = '''export default function AccountOnboarding() {
  const [form,    setForm]    = useState(INITIAL_FORM);'''

NEW_3 = '''export default function AccountOnboarding() {
  const [provider, setProvider] = useState("aws");
  const [form,    setForm]    = useState(INITIAL_FORM);'''

# ── Edit 4: insert tab bar + open AWS conditional, before onboard-hero ──

OLD_4 = '''  return (
    <div className="onboard-page">
      <div className="onboard-main">
        <div className="onboard-hero">
          <h1>Onboard <span className="hl">AWS Account</span></h1>
          <p>Register a new AWS account for centralized CloudWatch monitoring</p>
        </div>
'''

NEW_4 = '''  return (
    <div className="onboard-page">
      <div className="onboard-main">
        <div className="ob-provider-tabs">
          {PROVIDER_TABS.map(t => (
            <button
              type="button"
              key={t.id}
              className={`ob-provider-tab ${provider === t.id ? "ob-provider-tab-active" : ""}`}
              onClick={() => setProvider(t.id)}
            >
              {t.label}
            </button>
          ))}
        </div>

        {provider !== "aws" && <ComingSoon provider={provider} />}

        {provider === "aws" && (
        <>
        <div className="onboard-hero">
          <h1>Onboard <span className="hl">AWS Account</span></h1>
          <p>Register a new AWS account for centralized CloudWatch monitoring</p>
        </div>
'''

# ── Edit 5: close AWS conditional, right after the form closes ──

OLD_5 = '''        </form>
      </div>

      {/* Sidebar queue */}'''

NEW_5 = '''        </form>
        </>
        )}
      </div>

      {/* Sidebar queue */}'''


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not TARGET.exists():
        print(f"ABORT: {TARGET} not found. Run this from the project root.")
        sys.exit(1)

    print("AccountOnboarding.jsx")
    text = TARGET.read_text(encoding="utf-8")

    edits = [
        (OLD_1, NEW_1, "PROVIDER_TABS insertion point"),
        (OLD_2, NEW_2, "ComingSoon insertion point"),
        (OLD_3, NEW_3, "provider state insertion point"),
        (OLD_4, NEW_4, "tab bar + AWS conditional open"),
        (OLD_5, NEW_5, "AWS conditional close"),
    ]
    for old, new, label in edits:
        text = guarded_replace(text, old, new, label)

    if args.dry_run:
        print(f"  [dry-run] {len(edits)} anchors matched, would write changes.")
        print("\nDry run complete. No files written. Re-run without --dry-run to apply.")
        return

    backup(TARGET)
    TARGET.write_text(text, encoding="utf-8")
    print("  written.")

    if CSS_TARGET.exists():
        css = CSS_TARGET.read_text(encoding="utf-8")
        if CSS_MARKER in css:
            print("AccountOnboarding.css: tab styles already present, skipped.")
        else:
            backup(CSS_TARGET)
            CSS_TARGET.write_text(css + CSS_BLOCK, encoding="utf-8")
            print("AccountOnboarding.css: tab styles appended.")
    else:
        print(f"  NOTE: {CSS_TARGET} not found — skipped CSS append.")

    print("\nDone. Restart the frontend dev server and click through AWS / Azure / GCP tabs.")


if __name__ == "__main__":
    main()
