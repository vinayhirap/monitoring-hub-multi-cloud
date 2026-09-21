
/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!50503 SET NAMES utf8mb4 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `alerts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint DEFAULT NULL,
  `resource_id` varchar(512) NOT NULL,
  `metric_name` varchar(100) NOT NULL,
  `severity` varchar(20) NOT NULL,
  `current_value` double NOT NULL,
  `threshold` double NOT NULL,
  `status` varchar(20) NOT NULL,
  `silenced` tinyint(1) NOT NULL DEFAULT '0',
  `silenced_reason` varchar(500) DEFAULT NULL,
  `llm_summary` text,
  `llm_summary_source_hash` char(64) DEFAULT NULL,
  `llm_summary_generated_at` timestamp NULL DEFAULT NULL,
  `value` double DEFAULT NULL,
  `triggered_at` datetime NOT NULL,
  `resolved_at` datetime DEFAULT NULL,
  `last_seen_at` datetime DEFAULT NULL,
  `healthy_streak` int NOT NULL DEFAULT '0',
  `acked` tinyint(1) DEFAULT '0',
  `muted_until` datetime DEFAULT NULL,
  `escalated_at` timestamp NULL DEFAULT NULL,
  `escalated_to_group_id` bigint DEFAULT NULL,
  `marked_false_positive` tinyint(1) NOT NULL DEFAULT '0',
  `false_positive_marked_by` varchar(255) DEFAULT NULL,
  `false_positive_marked_at` timestamp NULL DEFAULT NULL,
  `environment` varchar(10) DEFAULT 'uat',
  `group_key` varchar(150) DEFAULT NULL,
  `region` varchar(20) DEFAULT NULL,
  `resolution_reason` varchar(60) DEFAULT NULL,
  `acked_by` varchar(100) DEFAULT NULL,
  `acked_at` datetime DEFAULT NULL,
  `resolved_by` varchar(100) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_alert_active` (`resource_id`,`metric_name`,`resolved_at`),
  KEY `idx_alerts_active` (`resolved_at`,`resource_id`,`metric_name`),
  KEY `idx_alerts_group_key` (`status`,`group_key`),
  KEY `idx_alerts_false_positive_lookup` (`resource_id`,`metric_name`,`marked_false_positive`),
  KEY `idx_alerts_account_resource_metric_status` (`aws_account_id`,`resource_id`,`metric_name`,`status`),
  KEY `idx_alerts_state_account` (`status`,`aws_account_id`,`severity`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `alert_pending` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `resource_id` varchar(500) NOT NULL,
  `metric_name` varchar(100) NOT NULL,
  `severity` varchar(20) NOT NULL,
  `environment` varchar(10) DEFAULT 'prod',
  `first_breach_at` datetime NOT NULL,
  `last_seen_at` datetime NOT NULL,
  `breach_cycles` int NOT NULL DEFAULT '1',
  `current_value` double DEFAULT NULL,
  `threshold_value` double DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_pending_account_resource_metric` (`aws_account_id`,`resource_id`,`metric_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `aws_accounts` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `provider` enum('aws','azure','gcp') NOT NULL DEFAULT 'aws',
  `account_name` varchar(100) NOT NULL,
  `account_id` varchar(20) NOT NULL,
  `role_arn` varchar(255) NOT NULL,
  `auth_mode` enum('assume_role','static_keys') NOT NULL DEFAULT 'assume_role',
  `external_id` varchar(100) DEFAULT NULL,
  `tenant_id` varchar(100) DEFAULT NULL,
  `subscription_id` varchar(100) DEFAULT NULL,
  `client_id` varchar(100) DEFAULT NULL,
  `project_id` varchar(100) DEFAULT NULL,
  `service_account_email` varchar(255) DEFAULT NULL,
  `credential_ref` varchar(255) DEFAULT NULL,
  `default_region` varchar(20) DEFAULT 'ap-south-1',
  `status` enum('active','inactive') DEFAULT 'active',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `description` varchar(255) DEFAULT NULL,
  `owner_team` varchar(100) DEFAULT '',
  `environment` varchar(20) DEFAULT 'PROD',
  `onboarded_by` bigint DEFAULT NULL,
  `last_synced_at` timestamp NULL DEFAULT NULL,
  `last_discovered_at` timestamp NULL DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_aws_accounts_last_discovered` (`last_discovered_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `resources` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `resource_type` varchar(20) NOT NULL,
  `normalized_resource_type` varchar(50) DEFAULT NULL,
  `resource_id` varchar(512) NOT NULL,
  `name` varchar(255) DEFAULT NULL,
  `tags` json DEFAULT NULL,
  `environment_id` bigint DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `last_seen_at` timestamp NULL DEFAULT NULL,
  `account_id` int DEFAULT NULL,
  `instance_state` varchar(20) DEFAULT 'unknown',
  `monitoring_tier` enum('critical','standard','low') NOT NULL DEFAULT 'standard',
  `region` varchar(50) DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_resource_identity` (`aws_account_id`,`resource_type`,`resource_id`),
  KEY `idx_resources_tier` (`aws_account_id`,`monitoring_tier`,`instance_state`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `metrics` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `resource_id` varchar(50) DEFAULT NULL,
  `metric_name` varchar(100) NOT NULL,
  `metric_value` double DEFAULT NULL,
  `metric_timestamp` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_metrics_resource_metric` (`resource_id`,`metric_name`),
  KEY `idx_metrics_lookup` (`resource_id`,`metric_name`,`metric_timestamp`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `metric_catalog` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `service` varchar(50) DEFAULT NULL,
  `provider` enum('aws','azure','gcp') NOT NULL DEFAULT 'aws',
  `metric_name` varchar(100) DEFAULT NULL,
  `namespace` varchar(100) DEFAULT NULL,
  `statistic` varchar(20) DEFAULT NULL,
  `unit` varchar(20) DEFAULT NULL,
  `default_interval` int DEFAULT NULL,
  `enabled` tinyint(1) DEFAULT '1',
  `display_service` varchar(150) DEFAULT NULL,
  `category` enum('core','extended','directory') NOT NULL DEFAULT 'extended',
  `description` varchar(255) DEFAULT NULL,
  `is_default` tinyint(1) NOT NULL DEFAULT '0',
  PRIMARY KEY (`id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `thresholds` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `resource_type` varchar(50) NOT NULL,
  `metric_id` bigint NOT NULL,
  `warning_value` double NOT NULL,
  `critical_value` double NOT NULL,
  `comparison` enum('>','<','>=','<=') NOT NULL,
  `evaluation_period` int NOT NULL DEFAULT '5',
  `enabled` tinyint(1) DEFAULT '1',
  `use_dynamic` tinyint(1) NOT NULL DEFAULT '0',
  `dynamic_k` double NOT NULL DEFAULT '3',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_threshold` (`aws_account_id`,`resource_type`,`metric_id`),
  UNIQUE KEY `uniq_acc_metric` (`aws_account_id`,`metric_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `metric_baseline` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `resource_id` varchar(512) NOT NULL,
  `metric_name` varchar(150) NOT NULL,
  `hour_of_day` tinyint NOT NULL,
  `day_of_week` tinyint NOT NULL,
  `mean_value` double NOT NULL,
  `stddev_value` double NOT NULL DEFAULT '0',
  `sample_count` int NOT NULL,
  `computed_by` enum('sigma_clip','stl') NOT NULL DEFAULT 'sigma_clip',
  `updated_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_account_baseline_bucket` (`aws_account_id`,`resource_id`,`metric_name`,`hour_of_day`,`day_of_week`),
  KEY `idx_baseline_lookup` (`resource_id`,`metric_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `maintenance_windows` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `resource_id` varchar(512) NOT NULL,
  `reason` varchar(500) NOT NULL,
  `starts_at` datetime NOT NULL,
  `ends_at` datetime NOT NULL,
  `silence_downstream` tinyint(1) NOT NULL DEFAULT '1',
  `created_by` varchar(100) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_maintenance_window` (`aws_account_id`,`starts_at`,`ends_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `resource_relationships` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `source_resource_id` varchar(512) NOT NULL,
  `target_resource_id` varchar(512) NOT NULL,
  `relationship_type` varchar(50) NOT NULL,
  `source` varchar(10) NOT NULL DEFAULT 'auto',
  `edge_hash` char(64) GENERATED ALWAYS AS (sha2(concat_ws(_utf8mb4':',`aws_account_id`,`source_resource_id`,`target_resource_id`,`relationship_type`),256)) STORED,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_edge_hash` (`edge_hash`),
  KEY `idx_rel_account` (`aws_account_id`),
  CONSTRAINT `fk_rel_account` FOREIGN KEY (`aws_account_id`) REFERENCES `aws_accounts` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `account_metric_selections` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `metric_id` bigint NOT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `source` varchar(20) DEFAULT 'template',
  PRIMARY KEY (`id`),
  UNIQUE KEY `u` (`aws_account_id`,`metric_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `status_page_components` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint NOT NULL,
  `name` varchar(100) NOT NULL,
  `resource_ids` json NOT NULL,
  `display_order` int NOT NULL DEFAULT '0',
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `created_by` varchar(100) DEFAULT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_status_page_account` (`aws_account_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `resource_health` (
  `resource_id` varchar(512) NOT NULL,
  `aws_account_id` bigint NOT NULL,
  `health_score` tinyint NOT NULL,
  `score_reason` json DEFAULT NULL,
  `computed_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`aws_account_id`,`resource_id`),
  KEY `idx_resource_health_account` (`aws_account_id`,`health_score`),
  CONSTRAINT `fk_resource_health_account` FOREIGN KEY (`aws_account_id`) REFERENCES `aws_accounts` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `escalation_policies` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `aws_account_id` bigint DEFAULT NULL,
  `severity` enum('WARNING','CRITICAL') NOT NULL,
  `ack_sla_minutes` int NOT NULL,
  `escalate_to_group_id` bigint NOT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '1',
  `created_by` bigint NOT NULL,
  `created_at` timestamp NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uniq_policy_scope` (`aws_account_id`,`severity`),
  KEY `fk_esc_group` (`escalate_to_group_id`),
  CONSTRAINT `fk_esc_account` FOREIGN KEY (`aws_account_id`) REFERENCES `aws_accounts` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_esc_group` FOREIGN KEY (`escalate_to_group_id`) REFERENCES `org_groups` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!50503 SET character_set_client = utf8mb4 */;
CREATE TABLE `org_groups` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `name` varchar(150) COLLATE utf8mb4_unicode_ci NOT NULL,
  `level` enum('L1','L2','L3') COLLATE utf8mb4_unicode_ci NOT NULL,
  `parent_group_id` bigint DEFAULT NULL,
  `description` varchar(500) COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `created_by` bigint NOT NULL,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_org_groups_name` (`name`),
  KEY `fk_org_groups_created_by` (`created_by`),
  KEY `idx_org_groups_parent` (`parent_group_id`),
  KEY `idx_org_groups_level` (`level`),
  CONSTRAINT `fk_org_groups_created_by` FOREIGN KEY (`created_by`) REFERENCES `users` (`id`) ON DELETE RESTRICT,
  CONSTRAINT `fk_org_groups_parent` FOREIGN KEY (`parent_group_id`) REFERENCES `org_groups` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
/*!40101 SET character_set_client = @saved_cs_client */;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

