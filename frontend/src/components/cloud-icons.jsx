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

// Real AWS and Google Cloud brand marks. Extracted verbatim from
// @iconify-json/logos (npm, icon keys "aws" and "google-cloud") the
// exact same way AzureBrandLogo above was -- the vendor-published
// brand SVG, not an invented shape or a per-service icon. Each
// preserves its own true aspect ratio (AWS's wordmark is wide,
// Google Cloud's mark is taller) rather than forcing a square box.
export function AwsBrandLogo({ size = 20, style, ...rest }) {
  const w = Math.round(size * (256 / 153));
  return (
    <svg width={w} height={size} viewBox="0 0 256 153" xmlns="http://www.w3.org/2000/svg" style={style} {...rest}>
      <path fill="#252f3e" d="M72.392 55.438c0 3.137.34 5.68.933 7.545a45.4 45.4 0 0 0 2.712 6.103c.424.678.593 1.356.593 1.95c0 .847-.508 1.695-1.61 2.543l-5.34 3.56c-.763.509-1.526.763-2.205.763c-.847 0-1.695-.424-2.543-1.187a26 26 0 0 1-3.051-3.984c-.848-1.44-1.696-3.052-2.628-5.001q-9.919 11.697-24.922 11.698c-7.12 0-12.8-2.035-16.954-6.103c-4.153-4.07-6.272-9.495-6.272-16.276c0-7.205 2.543-13.054 7.714-17.462c5.17-4.408 12.037-6.612 20.768-6.612c2.882 0 5.849.254 8.985.678c3.137.424 6.358 1.102 9.749 1.865V29.33c0-6.443-1.357-10.935-3.985-13.563c-2.712-2.628-7.29-3.9-13.817-3.9c-2.967 0-6.018.34-9.155 1.103s-6.188 1.695-9.155 2.882c-1.356.593-2.373.932-2.967 1.102s-1.017.254-1.356.254c-1.187 0-1.78-.848-1.78-2.628v-4.154c0-1.356.17-2.373.593-2.966c.424-.594 1.187-1.187 2.374-1.78q4.45-2.29 10.68-3.815C33.908.763 38.316.255 42.978.255c10.088 0 17.463 2.288 22.21 6.866c4.662 4.577 7.036 11.528 7.036 20.853v27.464zM37.976 68.323c2.798 0 5.68-.508 8.731-1.526c3.052-1.017 5.765-2.882 8.053-5.425c1.357-1.61 2.374-3.39 2.882-5.425c.509-2.034.848-4.493.848-7.375v-3.56a71 71 0 0 0-7.799-1.441a64 64 0 0 0-7.968-.509c-5.68 0-9.833 1.102-12.63 3.391s-4.154 5.51-4.154 9.748c0 3.984 1.017 6.951 3.136 8.986c2.035 2.119 5.002 3.136 8.901 3.136m68.069 9.155c-1.526 0-2.543-.254-3.221-.848c-.678-.508-1.272-1.695-1.78-3.305L81.124 7.799c-.51-1.696-.764-2.798-.764-3.391c0-1.356.678-2.12 2.035-2.12h8.307c1.61 0 2.713.255 3.306.848c.678.509 1.187 1.696 1.695 3.306l14.241 56.117l13.224-56.117c.424-1.695.933-2.797 1.61-3.306c.679-.508 1.866-.847 3.392-.847h6.781c1.61 0 2.713.254 3.39.847c.679.509 1.272 1.696 1.611 3.306l13.394 56.795L168.01 6.442c.508-1.695 1.102-2.797 1.695-3.306c.678-.508 1.78-.847 3.306-.847h7.883c1.357 0 2.12.678 2.12 2.119c0 .424-.085.848-.17 1.356s-.254 1.187-.593 2.12l-20.43 65.525q-.762 2.544-1.78 3.306c-.678.509-1.78.848-3.22.848h-7.29c-1.611 0-2.713-.254-3.392-.848c-.678-.593-1.271-1.695-1.61-3.39l-13.14-54.676l-13.054 54.59c-.423 1.696-.932 2.798-1.61 3.391c-.678.594-1.865.848-3.39.848zm108.927 2.289c-4.408 0-8.816-.509-13.054-1.526c-4.239-1.017-7.544-2.12-9.748-3.39c-1.357-.764-2.29-1.611-2.628-2.374a6 6 0 0 1-.509-2.374V65.78c0-1.78.678-2.628 1.95-2.628a4.8 4.8 0 0 1 1.526.255c.508.17 1.271.508 2.119.847a46 46 0 0 0 9.324 2.967a51 51 0 0 0 10.088 1.017c5.34 0 9.494-.932 12.376-2.797s4.408-4.577 4.408-8.053c0-2.373-.763-4.323-2.289-5.934s-4.408-3.051-8.561-4.408l-12.292-3.814c-6.188-1.95-10.765-4.832-13.563-8.647c-2.797-3.73-4.238-7.883-4.238-12.291q0-5.34 2.289-9.41c1.525-2.712 3.56-5.085 6.103-6.95c2.543-1.95 5.425-3.391 8.816-4.408c3.39-1.017 6.95-1.441 10.68-1.441c1.865 0 3.815.085 5.68.339c1.95.254 3.73.593 5.51.932c1.695.424 3.306.848 4.832 1.357q2.288.762 3.56 1.525c1.187.679 2.034 1.357 2.543 2.12q.763 1.017.763 2.797v3.984c0 1.78-.678 2.713-1.95 2.713c-.678 0-1.78-.34-3.22-1.018q-7.25-3.306-16.276-3.306c-4.832 0-8.647.763-11.275 2.374c-2.627 1.61-3.984 4.069-3.984 7.544c0 2.374.848 4.408 2.543 6.019s4.832 3.221 9.325 4.662l12.037 3.815c6.103 1.95 10.511 4.662 13.139 8.137s3.9 7.46 3.9 11.868c0 3.645-.764 6.951-2.205 9.833c-1.525 2.882-3.56 5.425-6.188 7.46c-2.628 2.119-5.764 3.645-9.409 4.747c-3.815 1.187-7.799 1.78-12.122 1.78" />
      <path fill="#f90" d="M230.993 120.964c-27.888 20.599-68.408 31.534-103.247 31.534c-48.827 0-92.821-18.056-126.05-48.064c-2.628-2.373-.255-5.594 2.881-3.73c35.942 20.854 80.276 33.484 126.136 33.484c30.94 0 64.932-6.442 96.212-19.666c4.662-2.12 8.646 3.052 4.068 6.442m11.614-13.224c-3.56-4.577-23.566-2.204-32.636-1.102c-2.713.34-3.137-2.034-.678-3.814c15.936-11.19 42.13-7.968 45.181-4.239c3.052 3.815-.848 30.008-15.767 42.554c-2.288 1.95-4.492.933-3.475-1.61c3.39-8.393 10.935-27.296 7.375-31.789" />
    </svg>
  );
}

export function GoogleCloudBrandLogo({ size = 20, style, ...rest }) {
  const w = Math.round(size * (256 / 206));
  return (
    <svg width={w} height={size} viewBox="0 0 256 206" xmlns="http://www.w3.org/2000/svg" style={style} {...rest}>
      <path fill="#ea4335" d="m170.252 56.819l22.253-22.253l1.483-9.37C153.437-11.677 88.976-7.496 52.42 33.92C42.267 45.423 34.734 59.764 30.717 74.573l7.97-1.123l44.505-7.34l3.436-3.513c19.797-21.742 53.27-24.667 76.128-6.168z" />
      <path fill="#4285f4" d="M224.205 73.918a100.25 100.25 0 0 0-30.217-48.722l-31.232 31.232a55.52 55.52 0 0 1 20.379 44.037v5.544c15.35 0 27.797 12.445 27.797 27.796c0 15.352-12.446 27.485-27.797 27.485h-55.671l-5.466 5.934v33.34l5.466 5.231h55.67c39.93.311 72.553-31.494 72.864-71.424a72.3 72.3 0 0 0-31.793-60.453" />
      <path fill="#34a853" d="M71.87 205.796h55.593V161.29H71.87a27.3 27.3 0 0 1-11.399-2.498l-7.887 2.42l-22.409 22.253l-1.952 7.574c12.567 9.489 27.9 14.825 43.647 14.757" />
      <path fill="#fbbc05" d="M71.87 61.426C31.94 61.663-.237 94.227.001 134.158a72.3 72.3 0 0 0 28.222 56.88l32.248-32.246c-13.99-6.322-20.208-22.786-13.887-36.776s22.786-20.208 36.775-13.888a27.8 27.8 0 0 1 13.887 13.888l32.248-32.248A72.22 72.22 0 0 0 71.87 61.427" />
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
