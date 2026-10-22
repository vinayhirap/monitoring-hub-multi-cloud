# Actual AWS cost (Cost Explorer, read-only)

The Cloud API cost page estimates cost from CloudOps's own counters (units x list price, free tiers ignored). This
feature adds what AWS **actually billed** for the CloudWatch service, shown next to the estimate.

## The one permission

`ce:GetCostAndUsage` and nothing else (`deploy/iam/monitoring-hub-cost-read.json`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    { "Sid": "MonitoringHubReadActualCost", "Effect": "Allow", "Action": "ce:GetCostAndUsage", "Resource": "*" }
  ]
}
```

- Read-only: it cannot create, change or delete anything.
- Cost Explorer has no resource-level restriction, so `Resource` must be `"*"`. The app narrows the query itself
  (service = AmazonCloudWatch only).
- Add it as an **inline policy on the role CloudOps assumes in each monitored AWS account** (the same role that carries
  `ReadOnlyAccess`). For an account onboarded with static keys, attach it to that IAM user instead.
- A member account sees only its own charges. A management (payer) account's role sees every linked account.

## Cost of asking

AWS bills **$0.01 per Cost Explorer API request**. The app never calls it on page load: it refreshes at most once per
24 hours per account (about $0.30 per account per month), and the manual Refresh button is limited to once per hour per
account. Every refresh records how many requests it made; the page shows that total.

## Setup (per monitored AWS account)

1. In the account, enable Cost Explorer once (Billing console > Cost Explorer). AWS can take up to ~24 hours to
   prepare data the first time.
2. Add the inline policy above to the role. CLI example, run with credentials for that account:
   `aws iam put-role-policy --role-name <ROLE_NAME> --policy-name MonitoringHubReadActualCost --policy-document file://deploy/iam/monitoring-hub-cost-read.json`
3. If calls still return AccessDenied with the policy attached, check the account setting "IAM user and role access to
   Billing information" (only the account root user can change it).
4. Verify the permission without spending anything (read-only, free):
   `aws iam simulate-principal-policy --policy-source-arn <ROLE_ARN> --action-names ce:GetCostAndUsage`
   The decision must be `allowed`.

## Turning it on in CloudOps

Do **PROD only**: DEV does not poll metrics, so it has nothing to compare against.

1. Prod box: apply the migration, `/opt/monitoring-hub/venv/bin/python3 migrate.py status`, then
   `/opt/monitoring-hub/venv/bin/python3 migrate.py apply --all-pending` (applies `111_aws_actual_cost.sql`).
2. Prod box: add `ACTUAL_COST_ENABLED=1` to `/opt/monitoring-hub/app/.env`, then `sudo systemctl restart monitoring-hub`.
3. Open Administration > API Cost. Within one scheduler cycle each account shows a status: `ok`,
   `permission_missing` (policy not attached yet), or `not_enabled` (Cost Explorer not enabled yet).

## Reading the numbers

- Cost Explorer lags real usage by up to ~48 hours; the newest day is partial and expected to be lower than the estimate.
  Compare estimate and actual only for complete days.
- Usage types are shown raw (for example a `...CW:GMD-Metrics` type is GetMetricData). The friendly label is
  best-effort; an unrecognised type is labelled "Other CloudWatch charge", never dropped.
- The actual figure includes everything CloudWatch bills (alarms, custom metrics, logs, API requests), not only
  CloudOps's reads. The usage-type table shows how much of it is GetMetricData.
