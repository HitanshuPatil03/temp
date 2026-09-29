#!/usr/bin/env bash
# Stand up the project's datastore in WSL2 Ubuntu, for a Windows developer
# machine where Docker Desktop's privileged service cannot be started (it needs
# elevation; WSL does not).
#
# Same versions as docker-compose.yml — PostgreSQL 17 with pgvector 0.8.6 — and
# the same connection string, so nothing in the application or the test suite
# changes: WSL2 forwards the Windows host's localhost into the distro.
#
# Run as root inside the distro:
#     wsl -d Ubuntu -u root -- bash /mnt/c/path/to/infra/wsl-postgres-setup.sh
#
# Idempotent: safe to re-run.

set -euo pipefail

CLUSTER_VERSION=17
CONF_DIR="/etc/postgresql/${CLUSTER_VERSION}/main"
DEV_PASSWORD="mrip_dev_only"   # matches config.DEV_DB_PASSWORD; refused in prod

echo "==> Installing PostgreSQL ${CLUSTER_VERSION} and pgvector from PGDG"
export DEBIAN_FRONTEND=noninteractive
if ! dpkg -l "postgresql-${CLUSTER_VERSION}" >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y -qq curl ca-certificates gnupg lsb-release >/dev/null
  install -d /usr/share/postgresql-common/pgdg
  curl -fsS -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    https://www.postgresql.org/media/keys/ACCC4CF8.asc
  codename="$(lsb_release -sc)"
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${codename}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update -qq
  apt-get install -y -qq \
    "postgresql-${CLUSTER_VERSION}" \
    "postgresql-${CLUSTER_VERSION}-pgvector" >/dev/null
fi

echo "==> Configuring the cluster to accept connections from the Windows host"
# WSL2 runs the distro behind a virtual switch. A service listening only on the
# distro's 127.0.0.1 is not reliably reachable through localhost forwarding, so
# listen on every interface — the switch is host-only, so this is not a network
# exposure.
sed -i "s/^#\?listen_addresses.*/listen_addresses = '*'/" "${CONF_DIR}/postgresql.conf"

if ! grep -q "mrip dev" "${CONF_DIR}/pg_hba.conf"; then
  cat >> "${CONF_DIR}/pg_hba.conf" <<'HBA'

# mrip dev: password auth (scram) for host connections, so the API process
# running on Windows connects with the same URL a container would use.
host    all             all             0.0.0.0/0               scram-sha-256
host    all             all             ::/0                    scram-sha-256
HBA
fi

echo "==> Starting the cluster"
# WSL2 often runs without systemd, so drive pg_ctlcluster directly rather than
# systemctl. Already-running is not an error.
pg_ctlcluster "${CLUSTER_VERSION}" main start 2>/dev/null || true
sleep 2
pg_lsclusters

run_sql() { su - postgres -c "psql -v ON_ERROR_STOP=1 $*"; }

echo "==> Creating the mrip role and databases"
# SUPERUSER because the baseline migration runs `CREATE EXTENSION vector`, which
# an ordinary owner may not. A production deployment installs the extension once
# as an administrator and grants the application role no more than it needs.
if [ "$(su - postgres -c "psql -tAc \"select count(*) from pg_roles where rolname='mrip'\"")" = "0" ]; then
  run_sql "-c \"CREATE ROLE mrip LOGIN SUPERUSER CREATEDB PASSWORD '${DEV_PASSWORD}'\""
fi

for db in mrip mrip_test; do
  if [ "$(su - postgres -c "psql -tAc \"select count(*) from pg_database where datname='${db}'\"")" = "0" ]; then
    su - postgres -c "createdb -O mrip ${db}"
    echo "    created ${db}"
  fi
  run_sql "-d ${db} -c 'CREATE EXTENSION IF NOT EXISTS vector'"
done

echo "==> Ready"
su - postgres -c "psql -tAc 'select version()'"
su - postgres -c "psql -d mrip -tAc \"select 'pgvector ' || extversion from pg_extension where extname='vector'\""
cat <<'NEXT'

Connection string (unchanged from docker-compose):
  MRIP_DATABASE_URL=postgresql+psycopg://mrip:mrip_dev_only@127.0.0.1:5432/mrip

The cluster does not survive a WSL shutdown. Restart it with:
  wsl -d Ubuntu -u root -- pg_ctlcluster 17 main start
NEXT
