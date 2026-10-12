# Cloud Resource Tagging Standard

Version 1.0 - applies to AWS, Azure and GCP resources monitored by CloudOps / Monitoring Hub.

## 1. Why this exists

Monitoring Hub decides how loudly to alert, how often to poll and who to escalate to from a
resource's **environment**. It reads that from the resource itself. A tag is the only signal that
is explicit, machine-readable and owned by the team that built the resource. Names are a fallback
and guesswork is a last resort, so every resource must carry the tags below.

Monitoring Hub resolves a resource's environment in this order and records which step decided it
(shown as "Decided by" on the Environments page):

1. Manual choice saved by a user in the Environments page
2. The `Environment` tag
3. A whole-word environment token in the resource name (`prod`, `uat`, `dev`, ...)
4. The resource it is attached to (an EBS volume follows its EC2 instance)
5. The account-level default
6. Unknown - alerted at prod strength and listed under "Needs classification"

## 2. Mandatory tags

Keys are `PascalCase`. Values are lowercase, no spaces, `-` as the only separator.
Do not use the `aws:` prefix (reserved by AWS).

| Key | Allowed values | Meaning |
|---|---|---|
| `Environment` | `prod`, `uat`, `dev` (exactly these three) | Deployment tier. Drives alert severity, priority and polling. |
| `Application` | lowercase application slug, e.g. `u4rad-orthanc` | What the resource belongs to. |
| `Owner` | team slug or distribution list, e.g. `platform-team` | Who is accountable. A team, never a person. |
| `CostCenter` | the organisation's cost-centre code | Chargeback. |
| `Criticality` | `critical`, `high`, `medium`, `low` | Business impact if it fails. |
| `DataClassification` | `public`, `internal`, `confidential`, `restricted` | Handling rules for the data it holds. |
| `ManagedBy` | `terraform`, `cloudformation`, `manual` | How it is changed. `manual` is a review flag. |

## 3. Environment catalogue

One key, three values. Variants are expressed with a second tag, not with new values, so every
tool sees the same tiers.

| Environment | Tag | Includes | Alerting posture |
|---|---|---|---|
| Production | `Environment=prod` | live traffic, production DR / standby | full strength, shortest alert delay |
| UAT | `Environment=uat` | UAT, pre-prod, staging, QA | reduced polling, longer alert delay |
| Development | `Environment=dev` | development, test, sandbox, PoC | lowest polling, critical-class metrics only |

Variant tag (optional): `EnvironmentRole` = `primary`, `dr`, `staging`, `qa`, `test`, `sandbox`.
A disaster-recovery copy of production is `Environment=prod` + `EnvironmentRole=dr`. It is production
for alerting purposes.

Monitoring Hub also understands these aliases so older estates keep working, but new resources
should use only the standard values: `production`/`prd` (prod); `preprod`, `stage`, `staging`, `stg`, `qa`
(uat); `development`, `test`, `testing`, `sandbox` (dev). Alias keys read: `Environment`, `Env`, `Stage`,
`DeploymentEnvironment`, `AppEnv`, `EnvType`. Key case is ignored, so `environment` and `Environment`
are the same key to Monitoring Hub.

`Criticality=critical` is reserved for the priority bump planned in Monitoring Hub phase 4b
(a critical-tagged resource is raised one priority level). It is not read for alerting yet.

## 4. Resource naming convention

Names are the fallback when a tag is missing, so they must be unambiguous.

    <org>-<application>-<env>-<role>-<nn>        e.g.  u4rad-orthanc-prod-db-01

- The environment is one lowercase, hyphen-delimited word: `prod`, `uat` or `dev`.
- Exactly one environment word per name. Never `test-prod`, `uat-test-env` or `prod-to-dev-sync`;
  such names cannot be classified and land in "Needs classification".
- Do not glue the word into another (`prodapi`, `devbox`); it will not match, deliberately, so that
  words like `product` and `latest` are never mistaken for an environment.
- A copy that is not production gets the target environment in its name: restore a production
  volume into test as `u4rad-orthanc-dev-db-01-restore`, not `test-u4rad-prod-orthanc`.

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

- **Terraform (AWS):** set `default_tags` on the provider so every resource gets the standard tags.

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

1. **AWS Organizations tag policy** so `Environment` can only be `prod`, `uat` or `dev`, spelled exactly:

       {
         "tags": {
           "Environment": {
             "tag_key":   { "@@assign": "Environment" },
             "tag_value": { "@@assign": ["prod", "uat", "dev"] },
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
5. Activate `Environment`, `Application` and `CostCenter` as cost-allocation tags in Billing.

## 8. Rollout for an existing estate

1. Tag production first (`Environment=prod`), then UAT, then development.
2. Open Environments in CloudOps. "Needs classification" shows what is still unknown.
3. Fix at the source (tag or rename) wherever possible. Use the manual mark only for resources you
   cannot tag.
4. Treat every unknown resource as a defect: it is alerted like production until classified. Target:
   an onboarded account has no unknown resources within five working days.
5. Review the "Decided by" column monthly. The goal is that nearly every resource is decided by
   `Tag`, not `Name`, `Account default` or `Attached resource`.
