# Cloud Resource Tagging Standard

Version 2.0 (two-environment model) - applies to AWS, Azure and GCP resources monitored by CloudOps / Monitoring Hub.

## 1. Why this exists

Monitoring Hub decides how loudly to alert, how often to poll and who to escalate to from a
resource's **environment**. It reads that from the resource itself. A tag is the only signal that
is explicit, machine-readable and owned by the team that built the resource. Names are a fallback
and guesswork is a last resort, so every resource must carry the tags below.

Monitoring Hub resolves a resource's environment in this order and records which step decided it
(shown as "Decided by" on the Environments page):

1. Manual choice saved by a user in the Environments page
2. The `Environment` tag
3. A whole-word environment token in the resource name (`prod`, `nonprod`, `dev`, `uat`, ...)
4. The resource it is attached to (an EBS volume follows its EC2 instance)
5. The account-level default
6. Unplaced - treated as PROD (fail-safe, alerted at prod strength) and listed under "Needs classification"

## 2. Mandatory tags

Keys are `PascalCase`. Values are lowercase, no spaces, `-` as the only separator.
Do not use the `aws:` prefix (reserved by AWS).

| Key | Allowed values | Meaning |
|---|---|---|
| `Environment` | `prod`, `nonprod` (exactly these two) | Deployment tier (displayed PROD / NON-PROD). Drives alert severity, priority and polling. |
| `Application` | lowercase application slug, e.g. `u4rad-orthanc` | What the resource belongs to. |
| `Owner` | team slug or distribution list, e.g. `platform-team` | Who is accountable. A team, never a person. |
| `CostCenter` | the organisation's cost-centre code | Chargeback. |
| `Criticality` | `critical`, `high`, `medium`, `low` | Business impact if it fails. |
| `DataClassification` | `public`, `internal`, `confidential`, `restricted` | Handling rules for the data it holds. |
| `ManagedBy` | `terraform`, `cloudformation`, `manual` | How it is changed. `manual` is a review flag. |

### Which tags CloudOps actually uses

The standard lists seven tags for the whole organisation, but CloudOps reads only some of them. The
Environments page asks for these four and ignores the rest:

| Tag | Used by CloudOps for |
|---|---|
| `Environment` | alert strength, polling and "Needs classification" (required) |
| `Criticality` | priority bump: `critical` raises the alert priority one level (never above P1) |
| `Owner` | who answers for the resource |
| `Application` | what the resource belongs to |

`CostCenter`, `DataClassification` and `ManagedBy` are cost and governance tags. CloudOps never reads them, so
it does not report them as missing; keep them wherever finance or security require them.

## 3. Environment catalogue

One key, two values. Variants are expressed with a second tag, not with new values, so every
tool sees the same two tiers.

| Environment | Tag | Includes | Alerting posture |
|---|---|---|---|
| PROD | `Environment=prod` | live traffic, production DR / standby | full strength: warning stays WARNING, critical P1 / other P2, normal polling |
| NON-PROD | `Environment=nonprod` | development, test, SIT, UAT, QA, staging, pre-prod, sandbox, PoC | warning-level breach is INFO, critical P3 / other P4, slower polling (when enabled), critical-class metrics evaluated first |

Variant tag (optional): `EnvironmentRole` = `primary`, `dr`, `uat`, `sit`, `staging`, `qa`, `test`, `sandbox`.
A disaster-recovery copy of production is `Environment=prod` + `EnvironmentRole=dr`. It is production
for alerting purposes.

Monitoring Hub also understands these aliases so older estates keep working, but new resources
should use only the standard values `prod` and `nonprod`. Accepted aliases: `production`/`prd` (prod);
`nonprod`-style spellings (`non-prod`, `non_prod`, `nonproduction`, `nonprd`), `dev`, `development`, `test`, `testing`,
`sit`, `uat`, `qa`, `qas`, `stage`, `staging`, `stg`, `preprod`, `preproduction`, `sandbox` (nonprod). Case, spaces, `-`, `_`
and a trailing number are ignored, so `Non-Prod`, `UAT-1` and `dev2` all resolve. Alias keys read: `Environment`, `Env`, `Stage`,
`DeploymentEnvironment`, `AppEnv`, `EnvType`. Key case is ignored, so `environment` and `Environment`
are the same key to Monitoring Hub.

`Criticality=critical` raises the alert priority of that resource one level (never above P1), in either
environment. It does not change alert severity.

### Two-environment model

CloudOps knows exactly two environments: **PROD** (stored `prod`) and **NON-PROD** (stored `nonprod`). Everything
else that used to be a tier (dev, test, SIT, UAT, QA, staging, preprod, sandbox) is NON-PROD.

- **Fail-safe.** Anything that cannot be placed (no tag, no recognisable name, an unknown free-text label, NULL, an
  old pre-migration value) is treated as **PROD**. An unclassified resource is therefore never alerted, ranked or
  polled more weakly than production. It also stays in the "Needs classification" queue (`env_source = 'unknown'`)
  until a person confirms it. The API rejects a value it cannot place instead of guessing.
- **migration 130** (`db/migrations/130_two_environment_model.sql`) converts stored data: `dev`, `test`, `sit`, `uat`,
  `qa`, `staging`, `preprod`, `sandbox` and their variants become `nonprod`; `prod`/`production`, `unknown`, `ignore` and
  unrecognised values become `prod`. Resources that were unknown or ignore keep `env_source = 'unknown'`. Every
  changed row is copied to `env_migration_130_backup`; the migration is idempotent and
  `db/migrations/130_two_environment_model_rollback.sql` restores the old values.
- **Behaviour change for former UAT / SIT resources.** They were a middle tier (priority P2/P3, polling x3). They now
  follow the NON-PROD policy: INFO at warning level, priority P3/P4, NON-PROD polling. If one is production-like, tag it
  `Environment=prod`.
- **Retag.** Existing `Environment=uat` / `dev` tags keep working as aliases, but retag to `nonprod` when convenient.

## 4. Resource naming convention

Names are the fallback when a tag is missing, so they must be unambiguous.

    <org>-<application>-<env>-<role>-<nn>        e.g.  u4rad-orthanc-prod-db-01

- The environment is one lowercase, hyphen-delimited word: `prod` for production; for anything else use `nonprod`
  or the specific word (`dev`, `test`, `uat`, `sit`, `qa`, `stg`), all of which are NON-PROD.
- Exactly one environment word per name. Never `test-prod`, `uat-test-env` or `prod-to-dev-sync`;
  such names cannot be classified and land in "Needs classification" (and are treated as PROD until placed).
- Do not glue the word into another (`prodapi`, `devbox`); it will not match, deliberately, so that
  words like `product` and `latest` are never mistaken for an environment.
- A copy that is not production gets the target environment in its name: restore a production
  volume into test as `u4rad-orthanc-nonprod-db-01-restore`, not `test-u4rad-prod-orthanc`.

## 5. Notation per cloud

| Cloud | Mechanism | Notes |
|---|---|---|
| AWS | Tags | Keys are case-sensitive; key up to 128 characters, value up to 256; 50 user tags per resource. |
| Azure | Tags | Names are case-insensitive, values case-sensitive; 50 tags per resource. |
| GCP | Labels | Lowercase letters, digits, `_` and `-` only, 63 characters max; use `environment`, `application`, `owner`, `costcenter`, `criticality`, `dataclassification`, `managedby`. |

Whether each cloud's tags or labels reach Monitoring Hub depends on that cloud's discovery storing
them on the resource; verify this per provider before relying on the tag step for Azure and GCP.

## 6. Make the tag appear everywhere

Child resources should not rely on inheritance; tag them at creation.

- **Terraform (AWS):** set `default_tags` on the provider so every resource gets the standard tags (`Environment = "nonprod"` for every non-production stack).

      provider "aws" {
        default_tags {
          tags = {
            Environment        = "prod"
            Application        = "u4rad-orthanc"
            Owner              = "platform-team"
            CostCenter         = "cc-1042"
            Criticality        = "high"
            DataClassification = "confidential"
            ManagedBy          = "terraform"
          }
        }
      }

- **EC2 volumes and ENIs:** tag on launch through the launch template's tag specifications for
  `instance` and `volume`.
- **Auto Scaling groups:** set the tag with propagate-at-launch.
- **Snapshots:** enable tag copying in the Data Lifecycle Manager policy.

## 7. Enforcement (AWS)

1. **AWS Organizations tag policy** so `Environment` can only be `prod` or `nonprod`, spelled exactly:

       {
         "tags": {
           "Environment": {
             "tag_key":   { "@@assign": "Environment" },
             "tag_value": { "@@assign": ["prod", "nonprod"] },
             "enforced_for": { "@@assign": ["ec2:instance", "ec2:volume", "rds:db"] }
           }
         }
       }

   Not every resource type supports enforcement; check the `enforced_for` list against the AWS
   documentation for the types you use.
2. **AWS Config** managed rule `required-tags` for the mandatory keys, reported as a compliance dashboard.
3. **Service control policy** that denies creating a resource without `aws:RequestTag/Environment`,
   where the service supports tagging at creation.
4. **Resource Groups Tag Editor** to back-tag existing resources in bulk, filtered by account and region.
5. Optional, for finance and not needed by CloudOps: activate `Environment`, `Application` and `CostCenter` as cost-allocation tags in Billing.

## 8. Rollout for an existing estate

1. Tag production first (`Environment=prod`), then everything else `Environment=nonprod`.
2. Open Environments in CloudOps. "Needs classification" shows what is still unknown.
3. Fix at the source (tag or rename) wherever possible. Use the manual mark only for resources you
   cannot tag.
4. Treat every unknown resource as a defect: it is treated as PROD (alerted like production) until classified. Target:
   an onboarded account has no unknown resources within five working days.
5. Review the "Decided by" column monthly. The goal is that nearly every resource is decided by
   `Tag`, not `Name`, `Account default` or `Attached resource`.

## 9. Keeping the classification trustworthy (Environments page)

- **Mismatches tab.** A resource whose tag, name, manual choice or attached instance point at different environments is listed with a
  one-line reason. A warning never changes the environment CloudOps uses.
- **Suggestions.** For resources nothing could place, the Needs-classification tab proposes an environment from already-placed siblings
  in the same account (same Application tag, stack or name pattern). Nothing is applied until a person accepts; accepted suggestions go
  through the normal audited classify call (recorded as "accepted suggestions").
- **Tag compliance tab.** Percentages per account and resource type by tag status and by how the environment was decided, a daily
  trend, and a CSV export. Types whose own AWS tags CloudOps does not read are "cannot be checked", never "untagged".
- **Tagging gaps export.** CSV of checkable resources missing any of the seven standard tags (Tagging tab).
- **Weekly digest (off by default).** `ENV_DIGEST_ENABLED=true` emails new unplaced resources to the members of the group whose name equals the
  account's owner team. `ENV_DIGEST_WEEKDAY` (0 = Monday) sets the earliest day.
