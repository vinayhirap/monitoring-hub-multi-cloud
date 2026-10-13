// src/pages/synthetic/certThresholds.js
// The certificate-expiry thresholds the BACKEND alert uses (SYNTHETIC_CERT_WARN_DAYS / SYNTHETIC_CERT_CRIT_DAYS,
// defaults 30 / 7) arrive on every https row from GET /synthetic as cert_warn_days / cert_crit_days. The table
// colours and the drawer's chart lines read them from here, so they cannot drift from the alert. The defaults
// below are only a fallback for an API that does not send them yet. Display only: alerts are raised server-side.
export const DEFAULT_CERT_WARN_DAYS = 30;
export const DEFAULT_CERT_CRIT_DAYS = 7;

/** { warn, crit } for a check row; falls back to the defaults unless the row has a sane pair (crit < warn). */
export function certThresholds(check) {
  const warn = Number(check?.cert_warn_days), crit = Number(check?.cert_crit_days);
  const ok = Number.isInteger(warn) && Number.isInteger(crit) && crit >= 1 && crit < warn;
  return ok ? { warn, crit } : { warn: DEFAULT_CERT_WARN_DAYS, crit: DEFAULT_CERT_CRIT_DAYS };
}

/** CSS class for days left on a certificate: "" (unknown), syn-cert-crit / -warn / -ok. */
export function certTone(days, th) {
  if (days == null) return "";
  if (days <= th.crit) return "syn-cert-crit";
  if (days <= th.warn) return "syn-cert-warn";
  return "syn-cert-ok";
}
