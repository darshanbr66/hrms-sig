# Sigvitas HRMS

Internal human resource management system for Sigvitas. Start with `CLAUDE.md` and the documents in `docs/`.

Status: Phase 1 (M1, foundation) in progress. See `docs/m1-implementation-plan.md`.

## Run it locally

To set up a development machine, start the services, create a local test sign-in and run the checks,
follow [`docs/local-development.md`](docs/local-development.md). That document is the only local
setup guide.

## Rules for migrations

- Alembic is the only migration runner and always runs as `hrms_migrator`.
- Revisions are expand/contract (`docs/database-design.md` §11).
- Never run `alembic downgrade` or `procrastinate schema --apply` against staging or production.
