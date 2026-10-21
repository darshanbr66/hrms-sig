# Sigvitas HRMS

Internal human resource management system for Sigvitas. Start with `CLAUDE.md` and the documents in `docs/`.

It is a Human Resource Management System (employee lifecycle, organization, attendance and
shifts, leave and holidays, documents, notifications, reports and exports, access
administration), not a recruitment platform.

Stack (ADR-030): FastAPI on Render, React on Vercel, MongoDB Atlas (database `hrms`) as the only
data store, Resend for email. No PostgreSQL, Redis, AWS or S3. Nothing is deployed yet
(`docs/deployment.md`).

Status: Phase 1 (M1, foundation) Checkpoints A to G are done; H (delivery) is proposed in
`docs/deployment.md`. Slices of M2 to M5 are built and verified locally but not yet committed, deployed or
accepted in a browser. Payroll and compensation, identifiers and
bank details are not built. See `docs/m1-implementation-plan.md` §12 for what is verified and
what remains, and `docs/roles-and-permissions.md` for who can do what.

## Run it locally

To set up a development machine, start the services, create a local test sign-in and run the checks,
follow [`docs/local-development.md`](docs/local-development.md). That document is the only local
setup guide.

## Rules for migrations

- `python -m app.cli migrate --confirm-database NAME` is the only migration runner, run as the
  migration database user. It creates collections, validators and indexes and applies numbered,
  idempotent data migrations (`migrations/__init__.py`); it never drops anything.
- Never edit or renumber a data migration that has run anywhere; add a new one.
- Local and test processes refuse the production database name `hrms`. Never point tests,
  `run-backend.cmd` or a local migration at the live Atlas database.
