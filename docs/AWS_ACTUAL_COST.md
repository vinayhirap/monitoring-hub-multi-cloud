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

## Cost Explorer view (migration 125)

Administration > Cloud Cost is a Cost Explorer style view of the actual bill: date range, daily or monthly, group by
(CloudOps vs everything else, account, region, usage type), account/region/cost-type filters, stacked cost graph,
usage graph, breakdown table and CSV. Every amount is a Cost Explorer figure; nothing is estimated or forecast.

- **Apply migration 125 first** (`migrate.py apply --all-pending`). It adds `usage_quantity` and `usage_unit` to
  `aws_cost_actuals` and `window_days` to `aws_cost_fetch_log`. Until it is applied the page still works on cost only.
- **Cost of checking.** An account's first read pulls 90 days (a one-off of a few $0.01 requests); every later read
  pulls 14 days, so steady state is about one request, $0.01, per account per day.
- **Who causes what.** CloudOps only reads CloudWatch (it never calls PutMetricData, PutMetricAlarm or any log or
  flow-log setting), so only `GMD-Metrics` (GetMetricData) is attributed to CloudOps polling. Custom metrics stored,
  alarms and `CW:Requests` (which includes PutMetricData from agents) are shown as "Agents, alarms and API requests";
  log ingestion/storage/S3 delivery as "Logs and log delivery". They are billed to the same account but are not
  created by CloudOps, and the page keeps them visible, only not as the headline.
- **Regions.** By default only the regions each account is onboarded in (its default region plus any region with
  discovered resources) are shown. Charges in other regions are not dropped: they are totalled in a line under the
  table so the page still reconciles to the AWS bill, and "Include other regions" shows them.
- **Cross-check.** For settled days the page compares the metrics AWS billed for GetMetricData with the metrics
  CloudOps's own meter counted. A large gap means the collector's metering needs attention.
- Tax (for example GST) is a separate AWS line and is not in these figures.
