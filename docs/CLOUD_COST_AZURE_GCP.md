# Cloud Cost for Azure and GCP

The Cloud Cost page (administrators) shows what CloudOps's own paid metric calls cost, as billed. AWS works through
Cost Explorer (see AWS_ACTUAL_COST.md). Azure and GCP use the readers in `app/cloud_cost.py`. They do nothing until an
active Azure subscription or GCP project exists and `ACTUAL_COST_ENABLED=1` is set; the page then shows an Azure / GCP
switch automatically. Apply migration 126 (`migrate.py apply --all-pending`).

**Not verified against a live Azure or GCP account when written.** The response shapes follow the vendors'
documentation and are unit tested with documented-shape fixtures. The first real refresh reports its outcome per
account on the page (permission missing, not set up, or a short error). Send that message if it is not "up to date".

## Azure

- Source: Cost Management Query API, `ActualCost`, daily, `MeterCategory = Azure Monitor`, grouped by meter and location.
- Permission: give the service principal CloudOps already uses the role **Cost Management Reader** on the subscription.
- Asking is free. A subscription billed in a local currency without USD columns is refused with a message instead of
  showing local-currency numbers as dollars.
- Only the metric API call meter is attributed to CloudOps (default pattern: a meter whose name mentions metric and
  query/API/call/request, excluding alerts, logs, ingestion, retention). If Microsoft names it differently, the page
  shows $0 CloudOps cost next to a non-zero "Not shown" total; set `AZURE_CLOUDOPS_METER_REGEX` to the real meter name.

## GCP

- GCP has no API that returns cost. Turn on **Billing > Billing export > Standard usage cost** to BigQuery (it only
  covers days after it is switched on), then set `GCP_BILLING_EXPORT_TABLE=project.dataset.table`.
- Permission: the service account needs `roles/bigquery.jobUser` on a project (set `GCP_BILLING_JOB_PROJECT` if it
  should not be the account's own project) and `roles/bigquery.dataViewer` on the export dataset.
- Credits, including the monthly free tier, are netted off, so the figure is what is actually charged.
- The query is a BigQuery job billed by bytes scanned; each refresh records that at list price
  (`BQ_PRICE_PER_TIB`, default 6.25). The first TiB a month is free at Google.
- Only the Monitoring API request SKU is attributed to CloudOps (`GCP_CLOUDOPS_SKU_REGEX` overrides it).

## Cost of asking

Azure: free. GCP: BigQuery bytes only. A first read pulls 90 days, later reads 14 days, at most once a day per account
(manual refresh once an hour).
