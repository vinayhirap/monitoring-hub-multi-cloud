# Tag sync: tag status for every resource type

Flag `TAG_SYNC_ENABLED` (default **off**). Migration 095. Code: `app/collector/tag_sync.py`,
`app/aws/arn_match.py` (pure matching), `scripts/tag_sync_report.py` (verification).

## What it does

Until now the Environments page could say whether a resource is tagged only for EC2, RDS, EFS, Auto Scaling,
Transit Gateway, VPN and unattached EBS volumes, because discovery stores the resource's real AWS tags only for
those. Every other type showed `not_checked`. With the flag on, after each discovery cycle CloudOps makes one
paginated Resource Groups Tagging API `GetResources` call series per account and region, matches each returned ARN to a
`resources` row, and stores the tags it found in `resources.synced_tags`. `tag_state` is then computed from those tags
for these types: S3, CloudWatch log groups, KMS, Lambda, load balancers, SQS, SNS, NAT gateways, ACM certificates,
CloudFront, Kinesis, WAFv2, Backup, EventBridge rules, DynamoDB, EKS, Firehose.

Attached EBS volumes are never touched (they are stored with their instance's tags on purpose).

## What a sweep can and cannot prove

| Sweep result | Meaning |
|---|---|
| ARN present | The resource **is tagged**, and its tags are known. Always safe to report. |
| ARN absent | Proves nothing by itself. AWS returns only resources that are tagged or were once tagged, and its index can lag. |

"Untagged" (`missing`) is therefore stated only when **all** of these hold (enforced in `arn_match.plan_account`):

1. The type is in `ABSENCE_VERIFIED_TYPES`: logs, kms, lambda, elb, sqs, sns, natgateway, certificatemanager, kinesis,
   backup, events, dynamodb, eks, firehose.
2. The sweep for the row's region finished: no error, no throttling, every page read. A failed or partial sweep is
   discarded completely and changes nothing.
3. The sweep returned at least one resource. A completely empty answer cannot be told from an index that has not caught
   up, so nothing is claimed from it. (A region where truly nothing is tagged is therefore reported `not_checked`, not
   `missing`, until at least one resource there is tagged.)
4. Canary: if the sweep contains ARNs of that type but **none** matched a row, the matching is suspect for that account
   and region and nothing is claimed for the type.
5. The row itself has an unambiguous key.

A tagged resource that fails to match is therefore never reported `missing`: failing to match is "unknown", never
"absent". Ambiguity is refused rather than guessed: two rows with the same key, the same ARN returned twice with
different tags, an ARN of another account, a Lambda alias or version ARN, an SNS subscription ARN, an EventBridge rule
on a custom bus, a WAF ACL whose id differs from the one discovery stored.

**Not allowed to claim "untagged" yet:** S3 (a bucket is indexed in its own region; the first PROD report left 17 of 46
AuroGov buckets unmatched, several named for ap-south-2, which that report never swept), CloudFront (global, us-east-1
only) and WAFv2 (no WAF ARN has appeared in any PROD sweep yet, so the ACL-name mapping is unproven). They still show
`ok / nonstandard / invalid / missing` when the sweep finds them (that is proof), and `not_checked` when it does not.
To promote one, run the report, confirm zero unexplained unmatched rows and ARNs, then add the type to
`ABSENCE_VERIFIED_TYPES` in a reviewed change. It cannot be switched on by an environment setting.

## Direct lookups for what the sweep cannot decide (`TAG_SYNC_DIRECT_LOOKUP`, off by default)

The Tagging-API index cannot prove that an S3 bucket or a WAFv2 ACL is untagged (absence from the index proves
nothing), so those rows stay `not_checked`. With this flag on, each sweep also reads the tags of just those undecided
rows directly, one call each:

| Type | Call | Proven result |
|---|---|---|
| S3 | `GetBucketTagging` in the bucket's own region | A tag set means tagged; error `NoSuchTagSet` means **untagged**. |
| WAFv2 | `ListTagsForResource` on the regional ACL's ARN (needs the `acl_id` discovery stored) | A tag list means tagged; an empty list means **untagged**. |

Any other outcome (AccessDenied, a region redirect, throttling after retries, a deleted resource, a missing `acl_id`)
leaves the row undecided, exactly as before. Rows the sweep already decided are never looked up, and a direct answer
never overrides a sweep result. At most `TAG_SYNC_DIRECT_MAX` rows per account per sweep. Cost on PROD today: about 17
buckets plus 2 ACLs = 19 calls per sweep, 19 x 24 = about 460 calls a day. This is a deliberate exception to the
"one paginated call per region" design: it is bounded, read-only, and only for rows nothing else can decide.

Permissions: `s3:GetBucketTagging` and `wafv2:ListTagsForResource`. The managed `ReadOnlyAccess` policy should include
both; this has not been confirmed against a live account, so run `scripts/tag_sync_report.py --direct` on PROD first. It
makes the same calls read-only and prints what the sync would store.

## Where the tags live, and why no merge into `resources.tags`

`resources.synced_tags` (JSON) and `resources.tags_synced_at` (migration 095). `{}` means "the sweep completed and the
resource was not in it". They are **not** merged into `resources.tags` because (a) discovery rewrites `tags` on every
run, so a merged result would vanish each cycle and a failed sweep would lose it; and (b) the legacy alert evaluator
reads `tags["Environment"]` directly, so a merge would change alert severity even with
`ENV_AWARE_CLASSIFICATION` off. Nothing reads the new columns while the flag is off.

A stored result older than `TAG_SYNC_MAX_AGE_HOURS` (default 6) is ignored, so a sweep that keeps failing degrades to
`not_checked` instead of freezing an old claim. Until then a failed sweep leaves the stored state untouched.
Tags starting `aws:` (CloudFormation and similar system tags) are dropped.

## Settings

| Setting | Default | Effect |
|---|---|---|
| `TAG_SYNC_ENABLED` | off | Everything above. Off: no AWS call, no query on the new columns, no change anywhere. |
| `TAG_SYNC_ABSENCE_TYPES` | unset = all verified types | Can only **narrow** the list: `none`, or `logs,kms`. It cannot add a type. |
| `TAG_SYNC_MAX_AGE_HOURS` | 6 | How long a stored result is trusted. |
| `TAG_SYNC_INTERVAL_MINUTES` | 60 (minimum 5) | How often the discovery hook sweeps. |
| `TAG_SYNC_DIRECT_LOOKUP` | off | For S3 buckets and WAFv2 ACLs the sweep could not decide, read their tags directly (see below). Needs `TAG_SYNC_ENABLED`. |
| `TAG_SYNC_DIRECT_MAX` | 100 | Most direct lookups per account per sweep. |
| `TAG_SYNC_FEED_ENVIRONMENT` | off | Also lets an `Environment` tag found by the sync decide the resource's environment (source `tag`). **This changes alert strength** (a bucket tagged `dev` becomes dev) and is off on purpose; turn it on separately, later. |

## AWS call volume

What the sync sweeps for one account (`tag_sync.sweep_plan`):

| Sweep | Scope | Why |
|---|---|---|
| The discovery region (e.g. ap-south-1), unfiltered | `full` | Proves tags for every type; the only sweep that can support an "untagged" claim. |
| us-east-1, `ResourceTypeFilters` = cloudfront, s3 | `global` | CloudFront is indexed in us-east-1 only. Filtered, so it is about one page instead of the hundreds of unrelated resources an unfiltered sweep returns. Only when the account has CloudFront or S3 rows. |
| Each other region that holds an S3 bucket row, filter s3 | `s3` | A bucket is indexed in its own region (for example ap-south-2). At most 10 regions. |

If AWS rejects a filter, that sweep is repeated unfiltered and only the types its scope may speak for are kept.

Measured on PROD with the first report (2026-10-17, unfiltered sweeps): AuroGov ap-south-1 = 623 resources in 7 pages,
U4RAD ap-south-1 = 739 resources in 8 pages; an unfiltered us-east-1 sweep returned 600 resources in 6 pages for
AuroGov, which is why that sweep is now filtered. With the plan above a cycle costs roughly 7 + 1 + 1 pages for AuroGov
and 8 + 1 pages for U4RAD, about 18 calls.

The sweep runs at most once per `TAG_SYNC_INTERVAL_MINUTES` (default 60), although discovery runs every 15 minutes:
tags change rarely and the stored result is trusted for 6 hours. 18 calls x 24 = **about 430 `GetResources` calls a day**
on prod (the first version of this document estimated less; the figure above comes from measured page counts). The
Tagging API has no charge. Calls are counted in the API usage log under provider `aws`, tier `tag_sync` (calls only,
no billed units). The sweep uses the shared adaptive retry config, so throttling slows it down rather than hammering AWS.

## Permissions

The monitoring role is assumed with the AWS managed `ReadOnlyAccess` policy (see `app/aws/sts.py`), which includes
`tag:GetResources`. Accounts onboarded with a narrower custom policy need `tag:GetResources` added. On
`AccessDenied` the sync logs one warning per account, region and day, and the extra types stay `not_checked`.
(`app/aws/federation.py` already lists `tag:GetResources` in its console session policy; that is a separate path.)

## How to verify before enabling (PROD, read-only)

1. Apply migration 095 (it only adds two NULLable columns).
2. Run the report: `scripts/tag_sync_report.py`. It reads the database and calls `GetResources`, and writes nothing.
3. For each type check: `badrows` (rows no ARN matched) and `badarns` (ARNs that matched no row). Resources that are
   truly untagged show up as **absent**, not as bad rows. A non-zero count needs an explanation (for example a deleted
   resource still listed by the index, or a NAT gateway discovery filters out). If a type shows its ARNs unmatched and
   its rows unmatched at the same time, the id mapping for that type is wrong: do not enable until it is fixed.
4. Look at the `tag_state` column. It is what the Environments page will show.
5. Enable: add `TAG_SYNC_ENABLED=true` to `.env`, restart, wait one discovery cycle, check the log line
   `[tag-sync] stored tag results for N resources in M API call(s)`.

## Roll back

Remove `TAG_SYNC_ENABLED` (or set it to `false`) and restart. At the next discovery cycle the classification pass
recomputes `tag_state` without the synced tags and the extra types return to `not_checked`. The two columns can stay;
nothing reads them. No data is lost and nothing was ever written to AWS.

## Limits

- The Tagging API index can lag behind a tag change by minutes. A resource tagged a minute ago may still show as untagged
  for a cycle or two.
- Types AWS does not return through `GetResources` stay `not_checked`.
- Only the discovery region of each account is swept in full, plus the cheap filtered sweeps above. A row of any other
  type in another region is `not_checked`. Resources in regions CloudOps does not discover (the first PROD report
  showed ACM, load balancers, KMS keys, log groups and rules in AuroGov's us-east-1) are not rows, so they are neither
  matched nor reported.
- Azure and GCP are not covered. Their discovery code is separate; whether it stores resource tags was not verified here.
