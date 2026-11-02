# Email & Reports centre

One place to see and control every email CloudOps sends. Built in patches; this file says what each one adds.

## Patch 0001 (foundation, this patch)

Off unless `EMAIL_CENTER_ENABLED=true` in `.env`. Apply migration 135 first (`migrate.py apply --all-pending`), then restart.

| Piece | What it does |
|---|---|
| `email_delivery_log` | One row per email handed to SMTP, from every feature (`app/email/mailer.py` writes it). Kept `EMAIL_LOG_RETENTION_DAYS` (90). |
| Recipient lists | Named lists of CloudOps users, org groups and typed addresses. A user's current email is read at send time; inactive users and users without an email are skipped; for account-specific mail, members who may not see the account are skipped. |
| Report schedules | Which report (type + scope) goes to which list, at what time. **Saved only; nothing sends them until patch 0002.** |
| `YEARLY` | New report type (rolling 365 days for a manual report). |
| API `/api/email-center/*` | `status`, `test`, `lists`, `schedules`, `log`. Permissions `emailcenter.view` (editor, admin) and `emailcenter.manage` (admin). |

SMTP stays in `.env`; the API shows its state and problems and can send a test email, but never returns the username or
password. A schedule can only be saved by someone who could generate that very report by hand.

## Later patches

- 0002: the scheduler (calendar-aligned weekly / monthly / quarterly / yearly periods, once per period, run as a system user).
- 0003: the Email & Reports page.
- 0004: alert and incident email rules (opened, escalated, resolved; severity or priority; account; list).
