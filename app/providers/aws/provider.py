"""
AWSProvider — CloudProvider implementation for AWS.

Deliberately a THIN WRAPPER: every method below delegates to the real,
existing AWS logic (app.aws.sts, app.aws.federation,
app.collector.discovery.runner, app.aws.metric_catalog_data). Nothing is
reimplemented here, so this file changing behavior for AWS is not
possible by construction — it just gives that existing logic a common
name other code (and eventually Azure/GCP) can call through.
"""
from app.providers.base import CloudProvider


class AWSProvider(CloudProvider):
    name = "aws"

    def validate_credentials(self, account: dict) -> dict:
        from app.aws.sts import assume_role

        role_arn = (account.get("role_arn") or "").strip()
        external_id = account.get("external_id")
        if not role_arn or not role_arn.startswith("arn:aws:"):
            raise ValueError("Valid IAM Role ARN required")

        session = assume_role(role_arn, external_id)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        return {
            "status": "success",
            "assumed_account": identity["Account"],
            "assumed_arn": identity["Arn"],
        }

    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None,
                         requested_by: str | None = None) -> str:
        from app.aws.federation import (
            build_federated_console_url,
            resource_console_destination,
        )

        destination = resource_console_destination(
            service, resource_id, region,
            resource_name=resource_name, ecs_service_name=ecs_service_name,
        )
        return build_federated_console_url(
            account.get("role_arn"), account.get("external_id"), destination,
            target_account_id=account.get("account_id"),
            requested_by=requested_by,
            service=service, resource_id=resource_id, region=region,
            resource_name=resource_name, ecs_service_name=ecs_service_name,
        )

    def discover_resources(self) -> None:
        # The real, live discovery path — same one the scheduler calls
        # every 15 minutes. NOT app.collector.discovery_ec2, which is
        # dead code (see the module docstring on that file).
        from app.collector.discovery.runner import run_discovery

        run_discovery()

    def get_metric_catalog(self) -> dict:
        from app.aws.metric_catalog_data import CURATED

        return CURATED
