"""
CloudProvider — the common interface every cloud adapter implements.

This mirrors the capability list from the multi-cloud architecture plan
(authenticate/validate, discover, get metrics, console URLs, etc.) but
only includes methods that have a concrete, currently-real AWS
implementation to wrap. Azure/GCP providers implement the same interface
in later steps; methods a given provider can't support should raise
NotImplementedError with a clear message rather than silently no-op or
fake a result (per the project's "do not fake support" principle).

Return values are plain dicts, matching the rest of this codebase's
convention (mysql.connector dictionary=True cursors) rather than
introducing a new dataclass style.
"""
from abc import ABC, abstractmethod


class CloudProvider(ABC):
    """Base interface for a cloud provider adapter (AWS, Azure, GCP, ...)."""

    #: short lowercase identifier, e.g. "aws" — must match the `provider`
    #: column value used in aws_accounts / metric_catalog.
    name: str

    @abstractmethod
    def validate_credentials(self, account: dict) -> dict:
        """
        Verify the credentials stored for `account` actually work.
        Returns a dict describing what was verified (e.g. the identity
        assumed). Raises on failure — callers turn that into an HTTP error.
        """
        raise NotImplementedError

    @abstractmethod
    def get_console_url(self, account: dict, resource_id: str, region: str,
                         service: str | None = None,
                         resource_name: str | None = None,
                         ecs_service_name: str | None = None) -> str:
        """
        Return a deep link into this provider's web console for the given
        resource, scoped to the correct account/subscription/project.

        `service` is the normalized/native resource-type key (e.g. "ec2",
        "ecs") and lets the provider dispatch precisely instead of
        guessing from `resource_id`'s shape. `resource_name` and
        `ecs_service_name` are optional extra identifiers some services
        need for a fully-specific deep link (AWS ELB search-by-name and
        ECS cluster>service, respectively) — providers that don't need
        them can ignore the arguments.
        """
        raise NotImplementedError

    @abstractmethod
    def discover_resources(self) -> None:
        """
        Run resource discovery for all active accounts of this provider.
        Side-effecting: writes/updates rows in the `resources` table, same
        contract as the existing scheduler-driven discovery.
        """
        raise NotImplementedError

    @abstractmethod
    def get_metric_catalog(self) -> dict:
        """
        Return this provider's metric catalog in the existing
        {service_key: (display_name, namespace, category, [metrics...])}
        shape used by app.aws.metric_catalog_data.CURATED.
        """
        raise NotImplementedError
