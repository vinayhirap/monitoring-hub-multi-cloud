import yaml
import mysql.connector

DB_CONFIG = {
    "host": "127.0.0.1",
    "port": 3307,
    "user": "root",
    "password": "YOUR_PASSWORD",
    "database": "monitoring_hub"
}

with open("config/health_rules.yaml") as f:
    rules = yaml.safe_load(f)

conn = mysql.connector.connect(**DB_CONFIG)
cur = conn.cursor(dictionary=True)

# Environments
env_map = {}
for env, tag in rules["environment_tags"].items():
    cur.execute(
        "INSERT IGNORE INTO environments (name, tag_key, tag_value) VALUES (%s,%s,%s)",
        (env, "environment", tag)
    )
    cur.execute("SELECT id FROM environments WHERE name=%s", (env,))
    env_map[env] = cur.fetchone()["id"]

# Thresholds
for env, metrics in rules["severity"].items():
    env_id = env_map[env]
    for metric, vals in metrics.items():
        cur.execute("""
            INSERT IGNORE INTO thresholds
            (metric_name, environment_id, warning, critical)
            VALUES (%s,%s,%s,%s)
        """, (metric, env_id, vals["warning"], vals["critical"]))

conn.commit()
print("Import completed successfully.")