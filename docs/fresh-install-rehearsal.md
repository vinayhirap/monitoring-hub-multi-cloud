# Rehearsing a fresh install on a scratch MySQL

Reproduces what `setup.sh` does to an empty database, without touching a server.
Used to find (and prove the fix for) the fresh-install login failure. MySQL 8 only --
MariaDB accepts syntax MySQL 8 rejects (`ADD COLUMN IF NOT EXISTS`).

```
# 1. scratch MySQL + the same names setup.sh uses
mysql -e "CREATE DATABASE monitoring_hub CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
          CREATE USER 'monitor'@'localhost' IDENTIFIED BY 'rehearsal_pw';
          GRANT ALL ON monitoring_hub.* TO 'monitor'@'localhost';"

# 2. a REAL clone (the apply scripts look for a .git DIRECTORY, so a worktree fails)
git clone https://github.com/vinayhirap/monitoring-hub-multi-cloud.git && cd monitoring-hub-multi-cloud
printf 'DB_HOST=127.0.0.1\nDB_PORT=3306\nDB_USER=monitor\nDB_PASSWORD=rehearsal_pw\nDB_NAME=monitoring_hub\nJWT_SECRET=x\n' > .env

# 3. base schema -- db_schema_only.sql is UTF-16, a plain grep will not read it
iconv -f utf-16 -t utf-8 db_schema_only.sql | sed 's/\r$//' | mysql -umonitor -prehearsal_pw monitoring_hub

# 4. the three users setup.sh seeds (step 7), then every run_migration step (step 8)
python3 - <<'PY'
import bcrypt, mysql.connector
c = mysql.connector.connect(host="127.0.0.1", user="monitor", password="rehearsal_pw", database="monitoring_hub"); cur = c.cursor()
for u, p, r in [("admin","admin123","admin"),("editor","editor123","editor"),("viewer","viewer123","viewer")]:
    cur.execute("INSERT INTO users (username, password, role, active) VALUES (%s,%s,%s,1)", (u, bcrypt.hashpw(p.encode(), bcrypt.gensalt()).decode(), r))
c.commit()
PY
for s in $(grep -E "^run_migration " setup.sh | awk '{print $2}'); do python3 "$s" || echo "FAILED $s"; done
git status --short | grep -v '^??'      # must print nothing: no script may modify tracked code

# 5. the migration step
python3 migrate.py bootstrap

# 6. prove it: boot the app and log in (expect HTTP 200, not 500)
env $(grep -v '^#' .env | xargs) COOKIE_SECURE=false python3 -m uvicorn app.main:app --port 8099 &
curl -s -o /dev/null -w "%{http_code}\n" -H 'Content-Type: application/json' \
     -d '{"username":"admin","password":"admin123"}' http://127.0.0.1:8099/api/auth/login
```

Optional: `MH_TEST_DB=1` runs the real-database tests (`tests/test_alert_lifecycle_integration.py`;
see its docstring for the `mh_test` database and `mh` user).
