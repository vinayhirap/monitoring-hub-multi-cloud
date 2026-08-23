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

Two tiers, mirroring the AWS/GCP files:

  CURATED   — 'core' = already collected by this app's resource
              collectors (VM, Storage Account, SQL Database, App
              Service); 'extended' = the next tier of commonly-monitored
              Azure services.

  DIRECTORY — remaining Azure resource types that publish platform
              metrics, registered by namespace only. Individual metric
              names are fetched on demand per account via Azure Monitor's
              metric-definitions API ("Discover" in the UI).

Run `python scripts/seed_multicloud_metric_catalog.py` after editing
this file to push changes into the `metric_catalog` table.
"""

CURATED = {

    # ── Core (already collected today) ───────────────────────────
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
        ("OS Disk Queue Depth",     "Count",   "Average", False, "OS disk queue depth — bottleneck signal"),
    ]),
    "storage_account": ("Azure Storage Accounts", "Microsoft.Storage/storageAccounts", "core", [
        ("UsedCapacity",         "Bytes",   "Average", True,  "Total storage used"),
        ("Transactions",         "Count",   "Total",   True,  "Total API transactions"),
        ("Ingress",              "Bytes",   "Total",   False, "Data ingress"),
        ("Egress",               "Bytes",   "Total",   False, "Data egress"),
        ("SuccessServerLatency", "MilliSeconds", "Average", True, "Server-side latency"),
        ("Availability",         "Percent", "Average", True,  "% successful requests"),
        ("SuccessE2ELatency",    "MilliSeconds", "Average", False, "End-to-end request latency"),
    ]),
    "sql_database": ("Azure SQL Database", "Microsoft.Sql/servers/databases", "core", [
        ("cpu_percent",             "Percent", "Average", True,  "% CPU used"),
        ("dtu_consumption_percent", "Percent", "Average", True,  "% DTU consumed (DTU-based tiers)"),
        ("storage_percent",         "Percent", "Average", True,  "% storage used"),
        ("connection_successful",   "Count",   "Total",   False, "Successful connections"),
        ("connection_failed",       "Count",   "Total",   True,  "Failed connections"),
        ("deadlock",                "Count",   "Total",   False, "Deadlocks"),
        ("blocked_by_firewall",     "Count",   "Total",   False, "Requests blocked by firewall"),
    ]),
    "app_service": ("Azure App Service", "Microsoft.Web/sites", "core", [
        ("CpuTime",             "Seconds", "Total",   False, "CPU time consumed"),
        ("Http5xx",             "Count",   "Total",   True,  "Server errors"),
        ("Requests",            "Count",   "Total",   True,  "Total requests"),
        ("AverageResponseTime", "Seconds", "Average", True,  "Average response time"),
        ("MemoryWorkingSet",    "Bytes",   "Average", False, "Memory in use"),
        ("Http4xx",             "Count",   "Total",   False, "Client errors"),
        ("HealthCheckStatus",   "Percent", "Average", True,  "Health-check pass rate"),
    ]),

    # ── Extended (common Azure services) ───────────────────────────
    "vmss": ("VM Scale Sets", "Microsoft.Compute/virtualMachineScaleSets", "extended", [
        ("Percentage CPU",     "Percent", "Average", True,  "% CPU used across the scale set"),
        ("Network In Total",   "Bytes",   "Total",   True,  "Inbound network traffic"),
        ("Network Out Total",  "Bytes",   "Total",   True,  "Outbound network traffic"),
        ("Disk Read Bytes",    "Bytes",   "Total",   False, "Disk read throughput"),
        ("Disk Write Bytes",   "Bytes",   "Total",   False, "Disk write throughput"),
    ]),
    "aks_cluster": ("Azure Kubernetes Service", "Microsoft.ContainerService/managedClusters", "extended", [
        ("node_cpu_usage_percentage",             "Percent", "Average", True,  "% CPU used per node"),
        ("node_memory_working_set_percentage",    "Percent", "Average", True,  "% memory used per node"),
        ("node_disk_usage_percentage",            "Percent", "Average", False, "% disk used per node"),
        ("kube_pod_status_phase",                 "Count",   "Average", True,  "Pod count by phase"),
        ("apiserver_current_inflight_requests",   "Count",   "Average", False, "API server in-flight requests"),
        ("kube_node_status_condition",             "Count",  "Average", False, "Node condition status"),
    ]),
    "function_app": ("Azure Functions", "Microsoft.Web/sites/functions", "extended", [
        ("FunctionExecutionCount", "Count",   "Total",   True,  "Function invocations"),
        ("FunctionExecutionUnits", "Count",   "Total",   True,  "Execution units consumed (memory×time)"),
        ("Http5xx",                "Count",   "Total",   True,  "Server errors"),
        ("AverageResponseTime",    "Seconds", "Average", False, "Average response time"),
    ]),
    "cosmosdb_account": ("Azure Cosmos DB", "Microsoft.DocumentDB/databaseAccounts", "extended", [
        ("TotalRequestUnits",        "Count",   "Total",   True,  "Total request units (RU) consumed"),
        ("NormalizedRUConsumption",  "Percent", "Average", True,  "% of provisioned RU/s consumed"),
        ("ServerSideLatency",        "MilliSeconds", "Average", True, "Server-side request latency"),
        ("TotalRequests",            "Count",   "Total",   False, "Total requests"),
        ("ProvisionedThroughput",    "Count",   "Average", False, "Provisioned RU/s"),
        ("DataUsage",                "Bytes",   "Average", False, "Data storage used"),
    ]),
    "redis_cache": ("Azure Cache for Redis", "Microsoft.Cache/Redis", "extended", [
        ("PercentProcessorTime",  "Percent", "Average", True,  "% CPU used"),
        ("UsedMemoryPercentage",  "Percent", "Average", True,  "% memory used"),
        ("connectedclients",      "Count",   "Average", True,  "Connected clients"),
        ("cachehits",             "Count",   "Total",   False, "Cache hits"),
        ("cachemisses",           "Count",   "Total",   False, "Cache misses"),
        ("serverLoad",            "Percent", "Average", False, "Server load"),
        ("evictedkeys",           "Count",   "Total",   False, "Keys evicted due to memory pressure"),
    ]),
    "service_bus_namespace": ("Azure Service Bus", "Microsoft.ServiceBus/namespaces", "extended", [
        ("IncomingMessages",   "Count", "Total",   True,  "Messages sent to the namespace"),
        ("OutgoingMessages",   "Count", "Total",   True,  "Messages delivered from the namespace"),
        ("ActiveMessages",     "Count", "Average", True,  "Messages waiting to be delivered"),
        ("ServerErrors",       "Count", "Total",   False, "Server-side errors"),
        ("ThrottledRequests",  "Count", "Total",   False, "Throttled requests"),
    ]),
    "eventhub_namespace": ("Azure Event Hubs", "Microsoft.EventHub/namespaces", "extended", [
        ("IncomingMessages",   "Count", "Total",   True,  "Messages received"),
        ("OutgoingMessages",   "Count", "Total",   True,  "Messages sent out"),
        ("IncomingBytes",      "Bytes", "Total",   False, "Ingress traffic"),
        ("ThrottledRequests",  "Count", "Total",   True,  "Throttled requests"),
    ]),
    "load_balancer": ("Azure Load Balancer", "Microsoft.Network/loadBalancers", "extended", [
        ("ByteCount",         "Bytes",   "Total",   True,  "Data processed"),
        ("PacketCount",       "Count",   "Total",   False, "Packets processed"),
        ("SYNCount",          "Count",   "Total",   False, "SYN packets received"),
        ("VipAvailability",   "Percent", "Average", True,  "Data-path availability"),
        ("DipAvailability",   "Percent", "Average", True,  "Backend instance availability"),
    ]),
    "application_gateway": ("Azure Application Gateway", "Microsoft.Network/applicationGateways", "extended", [
        ("Throughput",           "BytesPerSecond", "Average", True,  "Throughput"),
        ("TotalRequests",        "Count",          "Total",   True,  "Total requests"),
        ("UnhealthyHostCount",   "Count",          "Average", True,  "Unhealthy backend hosts"),
        ("ResponseStatus",       "Count",          "Total",   False, "Requests by response status code"),
        ("CurrentConnections",   "Count",          "Average", False, "Current connections"),
    ]),
    "key_vault": ("Azure Key Vault", "Microsoft.KeyVault/vaults", "extended", [
        ("ServiceApiHit",      "Count",        "Total",   True,  "API requests"),
        ("ServiceApiLatency",  "MilliSeconds", "Average", True,  "API request latency"),
        ("Availability",       "Percent",      "Average", True,  "% successful requests"),
        ("SaturationShoebox",  "Percent",      "Average", False, "Vault capacity utilization"),
    ]),
    "container_instance": ("Azure Container Instances", "Microsoft.ContainerInstance/containerGroups", "extended", [
        ("CpuUsage",                          "Count", "Average", True,  "CPU cores used"),
        ("MemoryUsage",                       "Bytes", "Average", True,  "Memory used"),
        ("NetworkBytesReceivedPerSecond",     "BytesPerSecond", "Average", False, "Inbound network throughput"),
        ("NetworkBytesTransmittedPerSecond",  "BytesPerSecond", "Average", False, "Outbound network throughput"),
    ]),
    "cdn_profile": ("Azure CDN / Front Door", "Microsoft.Cdn/profiles", "extended", [
        ("RequestCount",       "Count",        "Total",   True,  "Total requests"),
        ("ResponseSize",       "Bytes",        "Total",   False, "Response bytes served"),
        ("TotalLatency",       "MilliSeconds", "Average", True,  "Total request latency"),
        ("OriginRequestCount", "Count",        "Total",   False, "Requests forwarded to origin"),
    ]),
    "vpn_gateway": ("Azure VPN Gateway", "Microsoft.Network/virtualNetworkGateways", "extended", [
        ("TunnelAverageBandwidth", "BytesPerSecond", "Average", True,  "Average tunnel bandwidth"),
        ("TunnelIngressBytes",     "Bytes",          "Total",   False, "Tunnel ingress bytes"),
        ("TunnelEgressBytes",      "Bytes",          "Total",   False, "Tunnel egress bytes"),
        ("P2SConnectionCount",     "Count",          "Average", False, "Point-to-site connections"),
    ]),
    "data_factory": ("Azure Data Factory", "Microsoft.DataFactory/factories", "extended", [
        ("PipelineSucceededRuns", "Count", "Total", True,  "Successful pipeline runs"),
        ("PipelineFailedRuns",    "Count", "Total", True,  "Failed pipeline runs"),
        ("ActivitySucceededRuns", "Count", "Total", False, "Successful activity runs"),
        ("ActivityFailedRuns",    "Count", "Total", False, "Failed activity runs"),
    ]),
    "managed_disk": ("Azure Managed Disks", "Microsoft.Compute/disks", "extended", [
        ("Composite Disk Read Bytes/sec",  "BytesPerSecond", "Average", True,  "Disk read throughput"),
        ("Composite Disk Write Bytes/sec", "BytesPerSecond", "Average", True,  "Disk write throughput"),
        ("Composite Disk Read Operations/sec",  "CountPerSecond", "Average", False, "Read IOPS"),
        ("Composite Disk Write Operations/sec", "CountPerSecond", "Average", False, "Write IOPS"),
    ]),
}

# Namespace registered only — no hand-enumerated metric list (fetched live
# via Azure Monitor's metric-definitions API on demand, same pattern as the
# AWS DIRECTORY tier).
DIRECTORY = [
    ("Azure Cosmos DB (Cassandra/Mongo API)", "Microsoft.DocumentDB/databaseAccounts/cassandraKeyspaces"),
    ("Azure Kubernetes Service (node pools)", "Microsoft.ContainerService/managedClusters/agentPools"),
    ("Azure Synapse Analytics",     "Microsoft.Synapse/workspaces"),
    ("Azure Database for PostgreSQL", "Microsoft.DBforPostgreSQL/flexibleServers"),
    ("Azure Database for MySQL",    "Microsoft.DBforMySQL/flexibleServers"),
    ("Azure NetApp Files",          "Microsoft.NetApp/netAppAccounts/capacityPools/volumes"),
    ("Azure Traffic Manager",       "Microsoft.Network/trafficManagerProfiles"),
    ("Azure Firewall",              "Microsoft.Network/azureFirewalls"),
    ("Azure Front Door (classic)",  "Microsoft.Network/frontdoors"),
    ("Azure Logic Apps",            "Microsoft.Logic/workflows"),
    ("Azure Batch",                 "Microsoft.Batch/batchAccounts"),
    ("Azure Data Lake Storage Gen2","Microsoft.Storage/storageAccounts/blobServices"),
    ("Azure Notification Hubs",     "Microsoft.NotificationHubs/namespaces/notificationHubs"),
    ("Azure Machine Learning",      "Microsoft.MachineLearningServices/workspaces"),
    ("Azure Bastion",               "Microsoft.Network/bastionHosts"),
    ("Azure ExpressRoute Circuit",  "Microsoft.Network/expressRouteCircuits"),
    ("Azure Search",                "Microsoft.Search/searchServices"),
    ("Azure SignalR Service",       "Microsoft.SignalRService/SignalR"),
    ("Azure API Management",        "Microsoft.ApiManagement/service"),
    ("Azure Container Registry",    "Microsoft.ContainerRegistry/registries"),
]
