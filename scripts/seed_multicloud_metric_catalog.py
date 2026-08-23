# scripts/seed_multicloud_metric_catalog.py
"""
Seeds the metric_catalog table for Azure and GCP, same idempotent
ON DUPLICATE KEY pattern as scripts/seed_metric_catalog.py (AWS), but
explicitly tagging every row with its `provider` so it never collides
with or overwrites AWS's rows.

Usage:
    python scripts/seed_multicloud_metric_catalog.py
"""
import os
from dotenv import load_dotenv
load_dotenv()
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mysql.connector
from app.providers.azure.metric_catalog_data import CURATED as AZURE_CURATED, DIRECTORY as AZURE_DIRECTORY
from app.providers.gcp.metric_catalog_data import CURATED as GCP_CURATED, DIRECTORY as GCP_DIRECTORY

DB_CONFIG = dict(
    host=os.getenv("DB_HOST", "127.0.0.1"),
    port=int(os.getenv("DB_PORT", 3306)),
    user=os.getenv("DB_USER", "monitor"),
    password=os.getenv("DB_PASSWORD", "root123"),
    database=os.getenv("DB_NAME", "monitoring_hub"),
)


def seed_provider(cur, provider: str, curated: dict, directory: list) -> tuple[int, int]:
    curated_count = 0
    for service_key, (display_name, namespace, category, metrics) in curated.items():
        for metric_name, unit, statistic, is_default, description in metrics:
            interval = 60 if (category == "core" and is_default) else (300 if is_default else 900)
            cur.execute("""
                INSERT INTO metric_catalog
                    (service, namespace, display_service, metric_name,
                     statistic, unit, default_interval, category, description,
                     is_default, enabled, provider)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s)
                ON DUPLICATE KEY UPDATE
                    service          = VALUES(service),
                    display_service  = VALUES(display_service),
                    statistic        = VALUES(statistic),
                    unit             = VALUES(unit),
                    default_interval = VALUES(default_interval),
                    category         = VALUES(category),
                    description      = VALUES(description),
                    is_default       = VALUES(is_default),
                    provider         = VALUES(provider)
            """, (service_key, namespace, display_name, metric_name,
                  statistic, unit, interval, category, description, int(is_default), provider))
            curated_count += 1

    directory_count = 0
    curated_namespaces = {ns for _, ns, _, _ in curated.values()}
    for display_name, namespace in directory:
        if namespace in curated_namespaces:
            continue
        service_key = namespace.split(".")[0].split("/")[-1].lower().replace(" ", "-")
        cur.execute("""
            INSERT INTO metric_catalog
                (service, namespace, display_service, metric_name,
                 statistic, unit, default_interval, category, description,
                 is_default, enabled, provider)
            VALUES (%s,%s,%s,'',NULL,NULL,900,'directory',
                    'Namespace registered — metric names fetched on demand',
                    0,1,%s)
            ON DUPLICATE KEY UPDATE
                display_service  = VALUES(display_service),
                default_interval = VALUES(default_interval),
                provider         = VALUES(provider)
        """, (service_key, namespace, display_name, provider))
        directory_count += 1

    return curated_count, directory_count


def main():
    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()

    az_curated, az_dir = seed_provider(cur, "azure", AZURE_CURATED, AZURE_DIRECTORY)
    gcp_curated, gcp_dir = seed_provider(cur, "gcp", GCP_CURATED, GCP_DIRECTORY)

    conn.commit()
    cur.close()
    conn.close()
    print(f"Azure: seeded {az_curated} curated metrics, {az_dir} directory namespaces")
    print(f"GCP:   seeded {gcp_curated} curated metrics, {gcp_dir} directory namespaces")


if __name__ == "__main__":
    main()
