#!/usr/bin/env bash
# Day 14 OLTP baseline: reproduce the pre-optimization query plans.
#
# Creates a NEW disposable PostgreSQL 16 container, applies db/001-004 to an
# empty database, loads the deterministic synthetic fixture, runs
# VACUUM (ANALYZE), records the environment, captures the plans and removes
# the container. It never connects to any other database or container.
#
# Usage (repository root, bash or Git Bash, Docker running):
#   experiments/oltp-baseline/run_baseline.sh
# Environment overrides:
#   BASELINE_CONTAINER  container name (default eaiso-oltp-baseline; must not exist)
#   BASELINE_IMAGE      image (default postgres:16)
#   BASELINE_OUT        output directory (default experiments/oltp-baseline/raw)
#   BASELINE_REPEATS    warm repetitions per query after the first run (default 5)
#   BASELINE_PYTHON     Python with requirements-api.txt installed, for the
#                       supplementary application-path capture (default: skip)
#   KEEP_CONTAINER=1    leave the container running afterwards
set -euo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

HERE=$(cd "$(dirname "$0")" && pwd)
REPO=$(cd "$HERE/../.." && pwd)
CONTAINER=${BASELINE_CONTAINER:-eaiso-oltp-baseline}
IMAGE=${BASELINE_IMAGE:-postgres:16}
OUT=${BASELINE_OUT:-$HERE/raw}
REPEATS=${BASELINE_REPEATS:-5}
DB=eaiso_baseline
CPUS=2
MEMORY=2g
ORG_LARGE=8af08632-4147-4fce-49b8-0f34277f37f1
ORG_SMALL=ba163064-53d2-177c-e4e6-611ea72ea20e

# Capture the name list first: piping docker into `grep -q` under pipefail can
# fail on SIGPIPE and silently skip this refusal.
existing_names=$(docker ps -a --format '{{.Names}}')
if grep -qx -- "$CONTAINER" <<<"$existing_names"; then
    echo "refusing to run: container $CONTAINER already exists" >&2
    exit 1
fi
mkdir -p "$OUT"

# Superuser password for TCP access from the host (application-path capture
# only). Generated per run, held in memory, never written to disk or output.
PGPW=$(od -An -N16 -tx1 /dev/urandom | tr -d ' \n')

# Set only after `docker run` succeeds. Cleanup removes exactly that container
# ID, never a container found by name, so a pre-existing or concurrently
# created container with the same name is never removed.
CONTAINER_ID=""
cleanup() {
    if [ -n "$CONTAINER_ID" ] && [ "${KEEP_CONTAINER:-0}" != "1" ]; then
        # -v also removes the image's anonymous PGDATA volume.
        docker rm -f -v "$CONTAINER_ID" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

stamp() { date -u +%Y-%m-%dT%H:%M:%SZ; }

psql_db() {
    # Same per-connection timeouts the application sets (lock_timeout,
    # statement_timeout). Unix-socket connection inside the container.
    docker exec -i -e PGOPTIONS="-c lock_timeout=5000 -c statement_timeout=10000" \
        "$CONTAINER_ID" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d "$DB" "$@"
}

psql_setup() {
    # Setup (migrations, fixture, VACUUM) runs without the application's
    # timeouts so a slow host cannot abort it; measurements use psql_db.
    docker exec -i "$CONTAINER_ID" psql -X -q -v ON_ERROR_STOP=1 -U postgres -d "$DB" "$@"
}

wait_ready() {
    # The image's init phase listens on the Unix socket only; wait for TCP so
    # the final server (not the init server) is up.
    for _ in $(seq 1 60); do
        if docker exec "$CONTAINER_ID" pg_isready -q -h 127.0.0.1 -U postgres -d "$DB" 2>/dev/null; then
            return 0
        fi
        sleep 1
    done
    echo "PostgreSQL did not become ready" >&2
    exit 1
}

restart_pg() {
    # Empties shared_buffers. The host/VM page cache is NOT cleared.
    docker restart "$CONTAINER_ID" >/dev/null
    wait_ready
}

echo "[$(stamp)] starting $IMAGE as $CONTAINER (cpus=$CPUS memory=$MEMORY)"
CONTAINER_ID=$(docker run -d --name "$CONTAINER" --cpus "$CPUS" --memory "$MEMORY" \
    -e POSTGRES_PASSWORD="$PGPW" -e POSTGRES_DB="$DB" \
    -p 127.0.0.1::5432 "$IMAGE")
wait_ready

{
    echo "# Setup log"
    echo "started_utc: $(stamp)"
    for f in 001_init.sql 002_case_status_events.sql 003_actor_identity_and_grants.sql \
             004_case_creation_idempotency.sql; do
        psql_setup < "$REPO/db/$f"
        echo "applied db/$f (ON_ERROR_STOP=1): ok"
    done
    psql_setup < "$HERE/sql/fixture.sql"
    echo "loaded sql/fixture.sql: ok"
    psql_setup -c "VACUUM (ANALYZE);"
    echo "VACUUM (ANALYZE) on database $DB: ok"
    echo "finished_utc: $(stamp)"
} > "$OUT/setup-log.txt"

{
    echo "# Container and host resources (observable)"
    echo "captured_utc: $(stamp)"
    echo "image: $IMAGE"
    echo "image_id: $(docker image inspect "$IMAGE" --format '{{.Id}}')"
    echo "image_repo_digests: $(docker image inspect "$IMAGE" --format '{{json .RepoDigests}}')"
    echo "container_limits: $(docker inspect "$CONTAINER_ID" --format 'NanoCpus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}} ShmSize={{.HostConfig.ShmSize}}')"
    echo "cgroup_cpu_max: $(docker exec "$CONTAINER_ID" cat /sys/fs/cgroup/cpu.max 2>/dev/null || echo unavailable)"
    echo "cgroup_memory_max: $(docker exec "$CONTAINER_ID" cat /sys/fs/cgroup/memory.max 2>/dev/null || echo unavailable)"
    echo "container_nproc: $(docker exec "$CONTAINER_ID" nproc)"
    echo "container_kernel: $(docker exec "$CONTAINER_ID" uname -r)"
    echo "docker_host: $(docker info --format 'server={{.ServerVersion}} os={{.OperatingSystem}} ncpu={{.NCPU}} mem_bytes={{.MemTotal}}')"
    echo "storage: PGDATA on the image's anonymous Docker volume (no bind mount)"
    echo
    psql_db < "$HERE/sql/environment.sql"
} > "$OUT/environment.txt"

# One plan per fresh psql session: the application opens a new connection for
# every get_case()/list_cases() call. Run 1 follows a PostgreSQL restart
# (empty shared_buffers; OS/VM page cache uncontrolled); runs 2..N+1 are
# immediate repetitions (warm shared_buffers).
capture() {
    local name=$1 file=$2; shift 2
    local target="$OUT/$name.txt"
    {
        echo "# $name: raw EXPLAIN output from $file"
        echo "# database=$DB container=$CONTAINER image=$IMAGE"
        echo "# Each run is a separate psql session (new backend)."
    } > "$target"
    restart_pg
    local total=$((REPEATS + 1))
    for run in $(seq 1 "$total"); do
        if [ "$run" -eq 1 ]; then
            cache="first execution after PostgreSQL restart: shared_buffers empty; OS/VM page cache not controlled"
        else
            cache="repeat: shared_buffers warm from earlier runs"
        fi
        {
            echo
            echo "=== run $run of $total | started_utc $(stamp) | $cache ==="
            psql_db "$@" < "$HERE/$file"
        } >> "$target"
    done
}

echo "[$(stamp)] capturing plans"
capture query-a0-actor-status sql/query_a0_actor_status.sql
capture query-a-get-case sql/query_a_get_case.sql
capture query-b-list-cases-large-org sql/query_b_list_cases.sql -v org_id="$ORG_LARGE"
capture query-b-list-cases-small-org-supplementary sql/query_b_list_cases.sql -v org_id="$ORG_SMALL"

# Supplementary: the real application methods, plans logged by auto_explain.
if [ -n "${BASELINE_PYTHON:-}" ]; then
    echo "[$(stamp)] supplementary application-path capture"
    psql_db <<SQL
ALTER DATABASE $DB SET session_preload_libraries = 'auto_explain';
ALTER DATABASE $DB SET auto_explain.log_min_duration = 0;
ALTER DATABASE $DB SET auto_explain.log_analyze = on;
ALTER DATABASE $DB SET auto_explain.log_buffers = on;
ALTER DATABASE $DB SET auto_explain.log_verbose = on;
ALTER DATABASE $DB SET auto_explain.log_settings = on;
ALTER DATABASE $DB SET auto_explain.log_format = 'text';
SQL
    port=$(docker port "$CONTAINER_ID" 5432/tcp | head -1 | sed 's/.*://')
    since=$(docker exec "$CONTAINER_ID" date -u +%Y-%m-%dT%H:%M:%SZ)  # container clock, not host
    sleep 1
    # Repository-relative script path: a POSIX absolute path would not be
    # translated for a native Windows Python under Git Bash.
    (cd "$REPO" && BASELINE_DSN="host=127.0.0.1 port=$port dbname=$DB user=postgres password=$PGPW" \
        "$BASELINE_PYTHON" "${HERE#"$REPO"/}/capture_app_path.py") > "$OUT/app-path-client-output.txt"
    sleep 1
    {
        echo "# Supplementary: auto_explain log for the real get_case() and list_cases() calls"
        echo "# (capture_app_path.py, psycopg bound parameters). Server log since $since."
        docker logs --since "$since" "$CONTAINER_ID" 2>&1
    } > "$OUT/app-path-auto-explain.txt"
    # Expect exactly the three application statements (actor status, read, list).
    plans=$(grep -c "duration: .* plan:" "$OUT/app-path-auto-explain.txt" || true)
    if [ "$plans" -ne 3 ]; then
        echo "app-path capture: expected 3 auto_explain plans, found $plans" >&2
        exit 1
    fi
    psql_db -c "ALTER DATABASE $DB RESET ALL;"
fi

{
    echo "# Row counts after all captures (must equal environment.txt)"
    echo "captured_utc: $(stamp)"
    psql_db <<'SQL'
SELECT 'organization' AS table_name, count(*) AS row_count FROM organization
UNION ALL SELECT 'actor', count(*) FROM actor
UNION ALL SELECT 'organization_membership', count(*) FROM organization_membership
UNION ALL SELECT 'case_record', count(*) FROM case_record
UNION ALL SELECT 'case_status_event', count(*) FROM case_status_event
UNION ALL SELECT 'ai_suggestion', count(*) FROM ai_suggestion
UNION ALL SELECT 'case_creation_idempotency', count(*) FROM case_creation_idempotency
ORDER BY 1;
SELECT relname, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables ORDER BY relname;
SQL
} > "$OUT/final-row-counts.txt"

echo "[$(stamp)] done; raw output in $OUT"
