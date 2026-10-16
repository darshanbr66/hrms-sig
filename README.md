# Sigvitas HRMS

Internal human resource management system for Sigvitas. Start with `CLAUDE.md` and the documents in `docs/`.

It is a Human Resource Management System (employee lifecycle, organization, attendance and
shifts, leave and holidays, documents, notifications, reports and exports, access
administration), not a recruitment platform.

Status: Phase 1 (M1, foundation) Checkpoints A to G are done; H (delivery) waits for the hosting
decision. Slices of M2 to M5 are built and verified locally but not yet committed, deployed or
accepted in a browser. Payroll and compensation, identifiers and
bank details are not built. See `docs/m1-implementation-plan.md` §12 for what is verified and
what remains, and `docs/roles-and-permissions.md` for who can do what.

## Run it locally

To set up a development machine, start the services, create a local test sign-in and run the checks,
follow [`docs/local-development.md`](docs/local-development.md). That document is the only local
setup guide.

## Rules for migrations

- Alembic is the only migration runner and always runs as `hrms_migrator`.
- Revisions are expand/contract (`docs/database-design.md` §11).
- Never run `alembic downgrade` or `procrastinate schema --apply` against staging or production.
