# app/providers/azure/metric_catalog_data.py
"""
Curated Azure Monitor platform-metric catalog, same shape as
app.aws.metric_catalog_data.CURATED: service key -> (display name,
namespace, category, [ (metric_name, unit, statistic, is_default,
description), ... ]).

`namespace` here is the Azure Monitor metric namespace (Microsoft.*
resource-provider type), used both for display and to group resources by
type during discovery/console-link dispatch. Metric names/units are the
real Azure Monitor platform metric names — see
https://learn.microsoft.com/azure/azure-monitor/reference/supported-metrics/metrics-index
"""

CURATED = {
    "vm": ("Azure Virtual Machines", "Microsoft.Compute/virtualMachines", "core", [
        ("Percentage CPU",          "Percent", "Average", True,  "% CPU used"),
        ("Network In Total",        "Bytes",   "Total",   True,  "Inbound network traffic"),
        ("Network Out Total",       "Bytes",   "Total",   True,  "Outbound network traffic"),
        ("Disk Read Bytes",         "Bytes",   "Total",   False, "Disk read throughput"),
        ("Disk Write Bytes",        "Bytes",   "Total",   False, "Disk write throughput"),
        ("Disk Read Operations/Sec","CountPerSecond", "Average", False, "Disk read IOPS"),
        ("Disk Write Operations/Sec","CountPerSecond","Average", False, "Disk write IOPS"),
        ("VM Availability Metric",  "Count",   "Average", True,  "VM uptime/health signal"),
        ("CPU Credits Remaining",   "Count",   "Average", False, "Burstable (B-series) CPU credits available"),
    ]),
    "storage_account": ("Azure Storage Accounts", "Microsoft.Storage/storageAccounts", "core", [
        ("UsedCapacity",         "Bytes",   "Average", True,  "Total storage used"),
        ("Transactions",         "Count",   "Total",   True,  "Total API transactions"),
        ("Ingress",              "Bytes",   "Total",   False, "Data ingress"),
        ("Egress",               "Bytes",   "Total",   False, "Data egress"),
        ("SuccessServerLatency", "MilliSeconds", "Average", True, "Server-side latency"),
        ("Availability",         "Percent", "Average", True,  "% successful requests"),
    ]),
    "sql_database": ("Azure SQL Database", "Microsoft.Sql/servers/databases", "core", [
        ("cpu_percent",             "Percent", "Average", True,  "% CPU used"),
        ("dtu_consumption_percent", "Percent", "Average", True,  "% DTU consumed (DTU-based tiers)"),
        ("storage_percent",         "Percent", "Average", True,  "% storage used"),
        ("connection_successful",   "Count",   "Total",   False, "Successful connections"),
        ("connection_failed",       "Count",   "Total",   True,  "Failed connections"),
        ("deadlock",                "Count",   "Total",   False, "Deadlocks"),
    ]),
    "app_service": ("Azure App Service", "Microsoft.Web/sites", "core", [
        ("CpuTime",             "Seconds", "Total",   False, "CPU time consumed"),
        ("Http5xx",             "Count",   "Total",   True,  "Server errors"),
        ("Requests",            "Count",   "Total",   True,  "Total requests"),
        ("AverageResponseTime", "Seconds", "Average", True,  "Average response time"),
        ("MemoryWorkingSet",    "Bytes",   "Average", False, "Memory in use"),
    ]),
}

# Namespace registered only — no hand-enumerated metric list (fetched live
# via Azure Monitor's metric-definitions API on demand, same pattern as the
# AWS DIRECTORY tier).
DIRECTORY = [
    ("Azure Load Balancer",     "Microsoft.Network/loadBalancers"),
    ("Azure Application Gateway", "Microsoft.Network/applicationGateways"),
    ("Azure Key Vault",         "Microsoft.KeyVault/vaults"),
    ("Azure Cosmos DB",         "Microsoft.DocumentDB/databaseAccounts"),
    ("Azure Redis Cache",       "Microsoft.Cache/Redis"),
    ("Azure Functions",         "Microsoft.Web/sites/functions"),
    ("Azure Kubernetes Service","Microsoft.ContainerService/managedClusters"),
]
