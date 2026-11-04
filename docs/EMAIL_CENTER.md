# Email & Reports centre

One place to see and control every email CloudOps sends. Built in patches; this file says what each one adds.

## Patch 0001 (foundation, this patch)

Off unless `EMAIL_CENTER_ENABLED=true` in `.env`. Apply migration 135 first (`migrate.py apply --all-pending`), then restart.

| Piece | What it does |
|---|---|
| `email_delivery_log` | One row per email handed to SMTP, from every feature (`app/email/mailer.py` writes it). Kept `EMAIL_LOG_RETENTION_DAYS` (90). |
| Recipient lists | Named lists of CloudOps users, org groups and typed addresses. A user's current email is read at send time; inactive users and users without an email are skipped; for account-specific mail, members who may not see the account are skipped. |
| Report schedules | Which report (type + scope) goes to which list, at what time. Run by patch 0002. |
| `YEARLY` | New report type (rolling 365 days for a manual report). |
| API `/api/email-center/*` | `status`, `test`, `lists`, `schedules`, `log`. Permissions `emailcenter.view` (editor, admin) and `emailcenter.manage` (admin). |

SMTP stays in `.env`; the API shows its state and problems and can send a test email, but never returns the username or
password. A schedule can only be saved by someone who could generate that very report by hand.

## Patch 0002 (the scheduler)

One leader-guarded thread (`report-scheduler`, started when `EMAIL_CENTER_ENABLED=true`) checks the saved schedules every
minute. For each schedule that is due it claims the period (one atomic UPDATE, so a period is sent once), resolves the
recipients, generates the PDF with the normal report worker as the `scheduler` system user, emails it to each recipient
(one message each, each logged in the delivery log) and records SENT / PARTIAL / FAILED / SKIPPED on the schedule.

**Periods are the last full calendar period in the schedule's timezone** (default Asia/Kolkata):

| Type | Runs on | Covers |
|---|---|---|
| WEEKLY | a weekday | the Monday-to-Sunday week before the run |
| MONTHLY | day 1-28 | the previous calendar month |
| QUARTERLY | day 1-28 of Jan, Apr, Jul, Oct | the previous calendar quarter |
| YEARLY | day 1-28 of the chosen month | the 12 full months before the run month: month 1 = calendar year, month 4 = April to March (Indian financial year) |

- A run missed because the service was down is still sent while it is less than 36 hours late, never later.
- A schedule never sends for a run time earlier than its own creation.
- **Dev / no SMTP:** with `REPORTS_ENABLED=false` or SMTP unset the run stops before the PDF, writes one `skipped` delivery-log
  row per recipient with the reason, and records SKIPPED, so schedules, periods and recipients can be checked without S3 or mail.
- `REPORT_EMAIL_ALLOWED_DOMAINS` applies to scheduled mail too.
- `POST /api/email-center/schedules/{id}/run` runs a schedule now for its latest full period without using up the scheduled
  one; `{"dry_run": true}` only shows the period, the recipients and what would happen.
- The schedule list now includes `next_run_at`.

## Patch 0003 (the Email & Reports page)

Route `/email-center/:tab` (nav: Reports group, "Email & Reports"). It exists only where `EMAIL_CENTER_ENABLED=true`
(`GET /api/permissions/me` now returns `features.email_center`) and needs `emailcenter.view`; `emailcenter.manage` adds the
Recipients and Delivery log tabs and every write button. No new frontend dependency.

| Tab | Shows / does |
|---|---|
| SMTP | Server, security, sender, mail timezone, link base URL, allowed domains, whether reports are enabled; config problems; the plain-http `PUBLIC_APP_URL` warning (its own field, so it shows even while `SMTP_HOST` is unset); delivery counts for 24 h and 7 d; last failure; a test-email box (manage). |
| Recipients | Create / edit / delete lists with user, org-group and typed-address pickers; preview of who would really receive mail, optionally for one account. |
| Report schedules | Create / edit / pause / delete; report type, scope (account, resource, client, synthetic, cost, host, environment, security, SLO), day, time, timezone, list plus extra addresses; next run, last status and last error; Dry run and Run now. Polls every 5 s while a schedule is RUNNING. |
| Delivery log | Newest first; status and category filters; "Older" pages with `before_id`. |

Backend additions: `GET /api/email-center/options` (`emailcenter.view`) returns the accounts the caller can see, the scope
choices (each with an `allowed` flag for the caller) and the report types, weekdays and timezones. Users with an email and
org groups are returned only to `emailcenter.manage`, so a read-only viewer never receives an address. `GET /status` gains
`smtp.public_url_warning`. A help-center section "Email & Reports" was added.

## Patch 0004 (rule storage and API)

Migration 136 adds `email_alert_rules` (a rule: events, lowest severity, one account or all, recipient list, extra addresses)
and `email_rule_sends` (one row per mail sent, used for the once-per-event, throttle and hourly-cap checks).
`app/email/alert_rules.py` is pure: validation, matching and the throttle decision. API: `GET/POST /api/email-center/rules`,
`PUT/DELETE /api/email-center/rules/{id}` (`emailcenter.view` lists, `emailcenter.manage` changes; addresses are hidden from
viewers; a scoped user sees only rules for accounts they can see; a recipient list used by a rule cannot be deleted).

## Patch 0005 (sending the rule mail)

`app/email/rule_dispatch.py`. For each event it takes the enabled rules that match the event, severity and account and works
out who to mail:

- Recipients come from the rule's list and extra addresses through `recipients.resolve` (inactive users, users without an
  email and users who may not see the account are dropped).
- Never mailed here: an address an email notification channel already mails for that alert (opened / escalated), a member of
  the escalation group (escalated only), anyone for a silenced alert, and a person already mailed for that same alert or
  incident event by any rule. If the channel / group lookup fails, nothing is sent for that event.
- Limits: one mail per person per resource and event inside `EMAIL_RULE_THROTTLE_SECONDS` (1800), at most
  `EMAIL_RULE_MAX_PER_RECIPIENT_HOUR` (20) per rule and person per hour. The `email_rule_sends` row is written only after a
  successful send.
- Mail goes out as category `alert_rule` (visible in the Delivery log). Everything is a no-op unless `EMAIL_CENTER_ENABLED=true`.

**It never delays the collector.** The hooks only put a job on a bounded in-memory queue; one daemon worker thread does every
query and SMTP send. A sweep that is already queued or running is not queued again; a full queue drops the job.

| Event | Hook |
|---|---|
| alert opened | post-commit publish in `alert_evaluator.py` |
| alert escalated | `escalation.py`, after the commit |
| alert resolved, incident opened / resolved | one sweep per standard (5-minute) collector cycle in `scheduler.py`, looking back `EMAIL_RULE_SWEEP_MINUTES` (30) |

Alerts raised by synthetic checks, cost-jump and the host-agent watchdog were not covered by 0005 and are from patch 0007 (below).
Incidents correlated in the 15-minute tier are picked up within about 5 minutes.

## Patch 0006 (the rules tab, help and docs)

Frontend only, no new dependency, no new route, no migration. The Email & Reports page gets a fifth tab, **Alert & incident
rules** (`/email-center/rules`):

- List: name, events, severity, account, recipients (hidden without `emailcenter.manage`), Paused badge.
- `emailcenter.manage`: New rule, Edit, Pause / Enable, Delete (confirmed). The form picks events (five), severity
  (Warning and Critical, or Critical only), one account or all accounts, a recipient list and extra addresses. The backend stays
  the authority on every rule (including the account-access check).
- A "Good to know" panel states the limits above. The help-center section "Email & Reports" now covers the rules.

## Patch 0007 (the remaining alert sources)

Backend plus text only (no migration, no new setting). Three sources that already call the notification channels now also queue
a rule job after their own commit, through `rule_dispatch.enqueue_channel_event(kind, alert)` (the same non-blocking queue as 0005):

| Source | Hook | Events |
|---|---|---|
| synthetic checks (down, certificate expiry) | `synthetic_notify._fan_out_channels` | opened; escalated (a worse certificate stage) |
| cost-jump | `cost_jump_alert._notify` | opened (it never escalates or mails a resolve) |
| host-agent watchdog | `host_agent_watchdog._notify` | opened; escalated (offline after stale) |

Resolves are unchanged: the sweep already mailed them. Silenced and born-silenced alerts never reach the hooks.
Those sources already mail people of their own, so the worker also skips them (and sends nothing for the event if it cannot
read them): a synthetic check's contacts (outage or certificate topic; opened, escalated and resolved) and the cost-jump
recipients for the account (opened). A person is still mailed once per alert event whatever mix of rules, channels and escalation
matches, because the `email_rule_sends` key is per person, alert and event.

Still not covered: alerts from the cross-metric anomaly detector (`multivariate_anomaly.py`) send no opened rule mail (and no
channel mail either); their resolved mail is still sent by the sweep.

## Turning it on

1. `EMAIL_CENTER_ENABLED=true` and a working SMTP block in `.env`; apply migrations 135 and 136; restart.
2. Create a recipient list (Recipients tab), then a rule (Alert & incident rules tab) with one address of your own first.
3. Read the Delivery log (category `alert_rule`) after a real alert opens or resolves.

Before enabling on a live system, confirm against the real database: `alerts.resolved_at`, `alerts.silenced`,
`incidents.created_at` and `incidents.resolved_at` exist (0005 was verified against code and migrations, not a live DB).
