// frontend/src/components/cloud-icons.jsx
//
// Real icons only, per provider — no invented/generic SVGs.
//
//   AWS   -> @aws-icons/react   (mirrors AWS's official Architecture Icons set)
//   GCP   -> gcp-icons          (mirrors Google Cloud's official icon set)
//   Azure -> no equivalent official *per-service* icon package exists on the
//            npm/registries this environment can reach (unlike AWS/GCP,
//            Microsoft doesn't publish a redistributable per-resource icon
//            pack on npm). We use Microsoft's real Azure brand mark
//            (@iconify-json/logos, sourced from Azure's own branding) for
//            the provider badge, and Microsoft's own Fluent UI System Icons
//            (@fluentui/react-icons — an official Microsoft package, just
//            generic rather than per-service-branded) for individual Azure
//            service tiles, tinted in Azure blue. This is disclosed in the
//            UI via the `officialPerService` flag exported below so callers
//            can render an "Azure service icon" caveat if they want to.

import AmazonEc2Instance from "@aws-icons/react/resource/amazon-ec2-instance";
import AmazonElasticBlockStoreVolume from "@aws-icons/react/resource/amazon-elastic-block-store-volume";
import AmazonAuroraAmazonRdsInstance from "@aws-icons/react/resource/amazon-aurora-amazon-rds-instance";
import AmazonSimpleStorageServiceBucket from "@aws-icons/react/resource/amazon-simple-storage-service-bucket";
import AmazonElasticContainerServiceService from "@aws-icons/react/resource/amazon-elastic-container-service-service";
import ElasticLoadBalancingApplicationLoadBalancer from "@aws-icons/react/resource/elastic-load-balancing-application-load-balancer";
import AwsLambdaLambdaFunction from "@aws-icons/react/resource/aws-lambda-lambda-function";
import ElasticLoadBalancingNetworkLoadBalancer from "@aws-icons/react/resource/elastic-load-balancing-network-load-balancer";
import AmazonApiGateway from "@aws-icons/react/architecture-service/amazon-api-gateway";
import AmazonDynamoDb from "@aws-icons/react/architecture-service/amazon-dynamo-db";
import AmazonSimpleQueueService from "@aws-icons/react/architecture-service/amazon-simple-queue-service";
import AmazonSimpleNotificationService from "@aws-icons/react/architecture-service/amazon-simple-notification-service";
import AmazonCloudFront from "@aws-icons/react/architecture-service/amazon-cloud-front";
import AmazonElastiCache from "@aws-icons/react/architecture-service/amazon-elasti-cache";
import AmazonOpenSearchService from "@aws-icons/react/architecture-service/amazon-open-search-service";
import AmazonElasticKubernetesService from "@aws-icons/react/architecture-service/amazon-elastic-kubernetes-service";
import AmazonElasticFileSystemFileSystem from "@aws-icons/react/resource/amazon-elastic-file-system-file-system";
import AmazonDocumentDb from "@aws-icons/react/architecture-service/amazon-document-db";
import AmazonNeptune from "@aws-icons/react/architecture-service/amazon-neptune";
import AmazonManagedStreamingForApacheKafka from "@aws-icons/react/architecture-service/amazon-managed-streaming-for-apache-kafka";
import AmazonKinesisDataStreams from "@aws-icons/react/architecture-service/amazon-kinesis-data-streams";
import AmazonDataFirehose from "@aws-icons/react/architecture-service/amazon-data-firehose";
import AmazonEc2AutoScaling from "@aws-icons/react/architecture-service/amazon-ec2-auto-scaling";
import AmazonVpcNatGateway from "@aws-icons/react/resource/amazon-vpc-nat-gateway";
import AwsTransitGateway from "@aws-icons/react/architecture-service/aws-transit-gateway";
import AmazonRoute53 from "@aws-icons/react/architecture-service/amazon-route-53";
import AwsWaf from "@aws-icons/react/architecture-service/aws-waf";
import AmazonRedshift from "@aws-icons/react/architecture-service/amazon-redshift";
import AmazonMemoryDb from "@aws-icons/react/architecture-service/amazon-memory-db";
import AmazonDynamoDbAmazonDynamoDbAccelerator from "@aws-icons/react/resource/amazon-dynamo-db-amazon-dynamo-db-accelerator";
import AwsStepFunctions from "@aws-icons/react/architecture-service/aws-step-functions";
import AmazonEventBridge from "@aws-icons/react/architecture-service/amazon-event-bridge";
import AwsKeyManagementService from "@aws-icons/react/architecture-service/aws-key-management-service";
import AwsCertificateManager from "@aws-icons/react/architecture-service/aws-certificate-manager";
import AwsBackup from "@aws-icons/react/architecture-service/aws-backup";
import AmazonCognito from "@aws-icons/react/architecture-service/amazon-cognito";
import AmazonCloudWatchLogs from "@aws-icons/react/resource/amazon-cloud-watch-logs";
import AwsSiteToSiteVpn from "@aws-icons/react/architecture-service/aws-site-to-site-vpn";
import AwsGlobalAccelerator from "@aws-icons/react/architecture-service/aws-global-accelerator";
import AwsDatabaseMigrationService from "@aws-icons/react/architecture-service/aws-database-migration-service";
import AwsDirectConnect from "@aws-icons/react/architecture-service/aws-direct-connect";

import {
  ServerRegular,
  DatabaseRegular,
  CubeRegular,
  BoxRegular,
  FlashRegular,
  RouterRegular,
  DesktopTowerRegular,
  KeyRegular,
  MailRegular,
  GlobeRegular,
} from "@fluentui/react-icons";

// gcp-icons ships raw .svg files; Vite's default asset pipeline resolves a
// bare .svg import to a URL string, so these render via <img src=.../>.
import gcpCompute   from "gcp-icons/dist/icons/computeengine-512-color-rgb.svg";
import gcpStorage   from "gcp-icons/dist/icons/cloud-storage-512-color.svg";
import gcpSql       from "gcp-icons/dist/icons/cloudsql-512-color.svg";
import gcpRun       from "gcp-icons/dist/icons/cloudrun-512-color-rgb.svg";
import gcpGke       from "gcp-icons/dist/icons/gke-512-color.svg";
import gcpServerless from "gcp-icons/dist/icons/serverlesscomputing-512-color.svg";
import gcpIntegration from "gcp-icons/dist/icons/integrationservices-512-color.svg";
import gcpNetworking from "gcp-icons/dist/icons/networking-512-color-rgb.svg";
import gcpDatabases from "gcp-icons/dist/icons/databases-512-color.svg";
import gcpBigquery  from "gcp-icons/dist/icons/bigquery-512-color.svg";
import gcpSpanner   from "gcp-icons/dist/icons/cloudspanner-512-color.svg";
import gcpHyperdisk from "gcp-icons/dist/icons/hyperdisk-512-color.svg";

// Real Microsoft Azure logomark. Extracted verbatim from @iconify-json/logos
// (npm) — the same vendor-supplied brand SVG Microsoft publishes — rather
// than importing that package's full 2,100-icon / 7MB collection just to
// use one icon. If you ever need a different logos icon, pull its `body`
// from node_modules/@iconify-json/logos/icons.json the same way.
export function AzureBrandLogo({ size = 20, style, ...rest }) {
  return (
    <svg width={size} height={size} viewBox="0 0 256 242" xmlns="http://www.w3.org/2000/svg" style={style} {...rest}>
      <defs>
        <linearGradient id="azureLogoA" x1="58.972%" x2="37.191%" y1="7.411%" y2="103.762%">
          <stop offset="0%" stopColor="#114a8b" /><stop offset="100%" stopColor="#0669bc" />
        </linearGradient>
        <linearGradient id="azureLogoB" x1="59.719%" x2="52.691%" y1="52.313%" y2="54.864%">
          <stop offset="0%" stopOpacity=".3" /><stop offset="7.1%" stopOpacity=".2" />
          <stop offset="32.1%" stopOpacity=".1" /><stop offset="62.3%" stopOpacity=".05" />
          <stop offset="100%" stopOpacity="0" />
        </linearGradient>
        <linearGradient id="azureLogoC" x1="37.279%" x2="62.473%" y1="4.6%" y2="99.979%">
          <stop offset="0%" stopColor="#3ccbf4" /><stop offset="100%" stopColor="#2892df" />
        </linearGradient>
      </defs>
      <path fill="url(#azureLogoA)" d="M85.343.003h75.753L82.457 233a12.08 12.08 0 0 1-11.442 8.216H12.06A12.06 12.06 0 0 1 .633 225.303L73.898 8.219A12.08 12.08 0 0 1 85.343 0z" />
      <path fill="#0078d4" d="M195.423 156.282H75.297a5.56 5.56 0 0 0-3.796 9.627l77.19 72.047a12.14 12.14 0 0 0 8.28 3.26h68.02z" />
      <path fill="url(#azureLogoB)" d="M85.343.003a11.98 11.98 0 0 0-11.471 8.376L.723 225.105a12.045 12.045 0 0 0 11.37 16.112h60.475a12.93 12.93 0 0 0 9.921-8.437l14.588-42.991l52.105 48.6a12.33 12.33 0 0 0 7.757 2.828h67.766l-29.721-84.935l-86.643.02L161.37.003z" />
      <path fill="url(#azureLogoC)" d="M182.098 8.207A12.06 12.06 0 0 0 170.67.003H86.245c5.175 0 9.773 3.301 11.428 8.204L170.94 225.3a12.062 12.062 0 0 1-11.428 15.92h84.429a12.062 12.062 0 0 0 11.425-15.92z" />
    </svg>
  );
}

export const officialPerService = { aws: true, gcp: true, azure: false };

// ── AWS: real @aws-icons/react components ──────────────────────
const AWS_ICON = {
  // Core
  ec2:    AmazonEc2Instance,
  ebs:    AmazonElasticBlockStoreVolume,
  rds:    AmazonAuroraAmazonRdsInstance,
  alb:    ElasticLoadBalancingApplicationLoadBalancer,
  elb:    ElasticLoadBalancingApplicationLoadBalancer, // legacy alias
  lambda: AwsLambdaLambdaFunction,
  s3:     AmazonSimpleStorageServiceBucket,
  ecs:    AmazonElasticContainerServiceService,

  // Extended — matches the service keys in app/aws/metric_catalog_data.py
  nlb:                 ElasticLoadBalancingNetworkLoadBalancer,
  apigateway:          AmazonApiGateway,
  dynamodb:            AmazonDynamoDb,
  sqs:                 AmazonSimpleQueueService,
  sns:                 AmazonSimpleNotificationService,
  cloudfront:          AmazonCloudFront,
  elasticache:         AmazonElastiCache,
  opensearch:          AmazonOpenSearchService,
  eks:                 AmazonElasticKubernetesService,
  efs:                 AmazonElasticFileSystemFileSystem,
  documentdb:          AmazonDocumentDb,
  neptune:             AmazonNeptune,
  msk:                 AmazonManagedStreamingForApacheKafka,
  kinesis:             AmazonKinesisDataStreams,
  firehose:            AmazonDataFirehose,
  autoscaling:         AmazonEc2AutoScaling,
  natgateway:          AmazonVpcNatGateway,
  transitgateway:      AwsTransitGateway,
  route53:             AmazonRoute53,
  wafv2:               AwsWaf,
  redshift:            AmazonRedshift,
  memorydb:            AmazonMemoryDb,
  dax:                 AmazonDynamoDbAmazonDynamoDbAccelerator,
  states:              AwsStepFunctions,
  events:              AmazonEventBridge,
  kms:                 AwsKeyManagementService,
  certificatemanager:  AwsCertificateManager,
  backup:              AwsBackup,
  cognito:             AmazonCognito,
  logs:                AmazonCloudWatchLogs,
  vpn:                 AwsSiteToSiteVpn,
  globalaccelerator:   AwsGlobalAccelerator,
  dms:                 AwsDatabaseMigrationService,
  directconnect:       AwsDirectConnect,
};

export function AwsServiceIcon({ service, size = 32, color, style, ...rest }) {
  const Icon = AWS_ICON[service] || AmazonEc2Instance;
  return <Icon width={size} height={size} style={{ color, ...style }} {...rest} />;
}

// ── GCP: real gcp-icons SVG assets ──────────────────────────────
const GCP_ICON_URL = {
  compute_instance:        gcpCompute,
  gce_persistent_disk:     gcpHyperdisk,
  gcs_bucket:               gcpStorage,
  cloudsql_instance:        gcpSql,
  cloud_run_service:        gcpRun,
  gke_cluster:               gcpGke,
  gke_node:                  gcpGke,
  cloudfunctions_function:  gcpServerless,
  pubsub_topic:              gcpIntegration,
  pubsub_subscription:       gcpIntegration,
  cloud_lb:                  gcpNetworking,
  nat_gateway:               gcpNetworking,
  redis_instance:            gcpDatabases,
  firestore_database:        gcpDatabases,
  bigquery_project:          gcpBigquery,
  spanner_instance:          gcpSpanner,
};

export function GcpServiceIcon({ service, size = 32, style, ...rest }) {
  const src = GCP_ICON_URL[service] || gcpCompute;
  return (
    <img
      src={src}
      width={size}
      height={size}
      alt=""
      style={{ display: "inline-block", objectFit: "contain", ...style }}
      {...rest}
    />
  );
}

// ── Azure: real Fluent UI System Icons (generic, Microsoft-authored) ────
const AZURE_ICON = {
  vm:                    DesktopTowerRegular,
  vmss:                  DesktopTowerRegular,
  storage_account:       BoxRegular,
  sql_database:          DatabaseRegular,
  app_service:           GlobeRegular,
  aks_cluster:           CubeRegular,
  function_app:          FlashRegular,
  cosmosdb_account:      DatabaseRegular,
  redis_cache:           FlashRegular,
  service_bus_namespace: MailRegular,
  eventhub_namespace:    MailRegular,
  load_balancer:         RouterRegular,
  application_gateway:   RouterRegular,
  key_vault:             KeyRegular,
  container_instance:    CubeRegular,
  cdn_profile:           GlobeRegular,
  vpn_gateway:           RouterRegular,
  data_factory:          ServerRegular,
  managed_disk:          BoxRegular,
};

const AZURE_BLUE = "#0078D4";

export function AzureServiceIcon({ service, size = 32, color = AZURE_BLUE, style, ...rest }) {
  const Icon = AZURE_ICON[service] || ServerRegular;
  return <Icon fontSize={size} style={{ color, ...style }} {...rest} />;
}

// ── Unified dispatcher ───────────────────────────────────────────
export function CloudServiceIcon({ provider = "aws", service, size = 32, style }) {
  if (provider === "gcp")   return <GcpServiceIcon service={service} size={size} style={style} />;
  if (provider === "azure") return <AzureServiceIcon service={service} size={size} style={style} />;
  return <AwsServiceIcon service={service} size={size} style={style} />;
}
