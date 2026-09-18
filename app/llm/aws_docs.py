# app/llm/aws_docs.py
"""
Curated AWS documentation references for RCA reports (2026-09-18).
Every URL below was individually looked up and confirmed live against
docs.aws.amazon.com / repost.aws before being added here -- none of
this is generated or guessed. Only add an entry here after checking it
resolves; a dead or wrong link in a report is worse than no link.

Two tiers, matching how confidently we can name a page:
  - SERVICE_DOC_BY_RESOURCE_TYPE: the small set of resource types this
    app most commonly alerts on (ec2, ebs, rds, lambda, elb, ecs) get a
    precise, verified deep link straight to that service's "CloudWatch
    metrics for X" reference page.
  - Everything else (the 30+ extended-tier resource types -- backup,
    dynamodb, s3, sqs, and so on) falls back to _CLOUDWATCH_SERVICES_INDEX,
    AWS's own official index of every service that publishes CloudWatch
    metrics. It's one link, not a deep link, but it's genuinely correct
    for any of them -- better than fabricating 30+ specific URLs we
    haven't verified, or silently omitting the section for most alerts.

EC2_ISSUE_DOCS adds a second, metric-specific layer ONLY for EC2 (by
far the most common alerted resource type in this app) -- e.g. a
NetworkIn/NetworkOut alert also gets AWS's own network-performance
troubleshooting doc, not just the generic metrics reference.
"""

_CLOUDWATCH_SERVICES_INDEX = (
    "AWS services that publish CloudWatch metrics",
    "https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/aws-services-cloudwatch-metrics.html",
)

SERVICE_DOC_BY_RESOURCE_TYPE = {
    "ec2": ("CloudWatch metrics that are available for your EC2 instances",
            "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html"),
    "ebs": ("Amazon CloudWatch metrics for Amazon EBS",
            "https://docs.aws.amazon.com/ebs/latest/userguide/using_cloudwatch_ebs.html"),
    "eni": ("CloudWatch metrics that are available for your EC2 instances",
            "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/viewing_metrics_with_cloudwatch.html"),
    "rds": ("Monitoring Amazon RDS metrics with Amazon CloudWatch",
            "https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/monitoring-cloudwatch.html"),
    "lambda": ("Using CloudWatch metrics with Lambda",
               "https://docs.aws.amazon.com/lambda/latest/dg/monitoring-metrics.html"),
    "elb": ("CloudWatch metrics for your Application Load Balancer",
            "https://docs.aws.amazon.com/elasticloadbalancing/latest/application/load-balancer-cloudwatch-metrics.html"),
    "ecs": ("Monitor Amazon ECS using CloudWatch",
            "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cloudwatch-metrics.html"),
    "ecs_service": ("Monitor Amazon ECS using CloudWatch",
                     "https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cloudwatch-metrics.html"),
}

# Metric-name substrings (matched case-insensitively) -> an EC2-specific
# doc beyond the general metrics reference above. Checked one at a time
# against the actual metric names this app collects for EC2
# (NetworkIn/NetworkOut/NetworkPacketsIn/Out, CPUUtilization/
# CPUCreditBalance/CPUCreditUsage, StatusCheckFailed and its _System/
# _Instance variants).
_EC2_ISSUE_DOCS_BY_METRIC_SUBSTRING = [
    ("network", ("Monitor network performance for ENA settings on your EC2 instance",
                 "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-network-performance-ena.html")),
    ("statuscheck", ("Status checks for Amazon EC2 instances",
                      "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/monitoring-system-instance-status-check.html")),
    ("statuscheck", ("Troubleshoot Amazon EC2 instances with failed status checks",
                      "https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/TroubleshootingInstances.html")),
    ("cpu", ("AWS re:Post: How do I troubleshoot high CPU utilization on my EC2 instance?",
             "https://repost.aws/knowledge-center/ec2-troubleshoot-cpu-utilization")),
]


def get_references(resource_type: str, metric_name: str) -> list[dict]:
    """
    Returns a small, deduplicated list of {"title": ..., "url": ...}
    dicts -- always at least one (the service-level metrics doc,
    specific if we have it, otherwise AWS's own CloudWatch services
    index), plus any EC2 metric-specific doc that applies. Never
    empty, never fabricated -- see module docstring.
    """
    resource_type = (resource_type or "").lower()
    metric_name = (metric_name or "").lower()

    refs = []
    title, url = SERVICE_DOC_BY_RESOURCE_TYPE.get(resource_type, _CLOUDWATCH_SERVICES_INDEX)
    refs.append({"title": title, "url": url})

    if resource_type == "ec2":
        for substring, (issue_title, issue_url) in _EC2_ISSUE_DOCS_BY_METRIC_SUBSTRING:
            if substring in metric_name:
                refs.append({"title": issue_title, "url": issue_url})

    # Dedup while preserving order, in case a future metric matches the
    # same doc twice.
    seen = set()
    deduped = []
    for ref in refs:
        if ref["url"] not in seen:
            seen.add(ref["url"])
            deduped.append(ref)
    return deduped
