-- One-time cluster bootstrap for an HRMS environment (docs/database-design.md §3, §11).
--
-- Run once per environment by a database administrator, before the first migration:
--
--   psql "<admin connection>" \
--     -v db_name=hrms \
--     -v migrator_password="$HRMS_DB_MIGRATOR_PASSWORD" \
--     -v app_password="$HRMS_DB_APP_PASSWORD" \
--     -v worker_password="$HRMS_DB_WORKER_PASSWORD" \
--     -v audit_retention_password="$HRMS_DB_AUDIT_RETENTION_PASSWORD" \
--     -f infra/db/bootstrap-roles.sql
--
-- Passwords come from the secret store and are never committed. Everything else
-- (schemas, grants, default privileges, per-role timeouts) is applied by Alembic
-- running as hrms_migrator, so it is versioned with the code.

\set ON_ERROR_STOP on

-- Owner of the database and every schema object. Used only by the migration job.
-- CREATEROLE plus ADMIN OPTION (below) lets migrations set per-database role settings
-- for the application roles without being able to act as them.
CREATE ROLE hrms_migrator LOGIN CREATEROLE PASSWORD :'migrator_password';

-- API process.
CREATE ROLE hrms_app LOGIN PASSWORD :'app_password';

-- Worker process (jobs, audit sealer).
CREATE ROLE hrms_worker LOGIN PASSWORD :'worker_password';

-- Audit retention job only (docs/security-architecture.md §8.1).
CREATE ROLE hrms_audit_retention LOGIN PASSWORD :'audit_retention_password';

-- ADMIN OPTION without INHERIT or SET: the migrator can manage these roles' settings
-- but never gains their privileges or switches to them.
GRANT hrms_app TO hrms_migrator WITH ADMIN OPTION, INHERIT FALSE, SET FALSE;
GRANT hrms_worker TO hrms_migrator WITH ADMIN OPTION, INHERIT FALSE, SET FALSE;
GRANT hrms_audit_retention TO hrms_migrator WITH ADMIN OPTION, INHERIT FALSE, SET FALSE;

-- Creating a database owned by another role, and changing its privileges, needs the ability to
-- act as that role (PostgreSQL 16+). A superuser has it; a managed database's administrator
-- (CREATEROLE and CREATEDB, not superuser, as on Render) does not. So the administrator takes
-- SET on hrms_migrator for these statements only, without inheriting its privileges, and gives
-- it up at the end. If a statement fails, ON_ERROR_STOP leaves the grant in place: the
-- administrator could grant it to itself again anyway (it holds ADMIN OPTION on every role it
-- created), and a failed bootstrap is repeated on a fresh instance.
GRANT hrms_migrator TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;

CREATE DATABASE :"db_name" OWNER hrms_migrator ENCODING 'UTF8' TEMPLATE template0;

-- As the owner, so the revoke removes the owner's default grants to PUBLIC (CONNECT, TEMP).
SET ROLE hrms_migrator;
REVOKE ALL ON DATABASE :"db_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db_name" TO hrms_app, hrms_worker, hrms_audit_retention;
RESET ROLE;

REVOKE hrms_migrator FROM CURRENT_USER;
