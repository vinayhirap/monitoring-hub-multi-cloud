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

## Later patches

- 0004: alert and incident email rules (opened, escalated, resolved; severity or priority; account; list).
