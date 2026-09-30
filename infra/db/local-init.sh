#!/bin/sh
# First-start hook of the local PostgreSQL container (docker-entrypoint-initdb.d).
# Applies the same bootstrap as every other environment, with local passwords from .env.
set -eu

psql --username "$POSTGRES_USER" --dbname postgres \
  -v db_name=hrms \
  -v migrator_password="$HRMS_DB_MIGRATOR_PASSWORD" \
  -v app_password="$HRMS_DB_APP_PASSWORD" \
  -v worker_password="$HRMS_DB_WORKER_PASSWORD" \
  -v audit_retention_password="$HRMS_DB_AUDIT_RETENTION_PASSWORD" \
  -f /hrms-bootstrap/bootstrap-roles.sql
