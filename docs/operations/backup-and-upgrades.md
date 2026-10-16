# Backup and upgrades

## What to back up

All state is in the database. Gateways and workers are stateless. Configuration (environment variables and secrets)
lives in your deployment system and should be versioned there.

| Data | Tables | Why it matters |
|---|---|---|
| **Learned knowledge** | `skills`, `bullets`, `skill_versions`, `memories`, `profiles` | This is the product of learning, accumulated over weeks of traffic and LLM spend. It can't be regenerated without the episodes and the LLM cost. |
| **Governance record** | `changesets`, `experiments`, `audit_log` | Approvals, rejections (the rejected-edit buffer), canary history and the audit trail. Often a compliance requirement. |
| **Credentials** | `api_keys` | Losing it invalidates every issued key |
| **Evidence** | `episodes`, `recalls`, `agent_state`, `pair_decisions` | Needed for replay, evolution, lift measurement and export. Large. |
| Operational | `jobs`, `llm_usage` | Usage history for chargeback. The job queue is transient. |

Back up the whole database. If you must prioritise, knowledge, governance and credentials are small and hardest to
replace.

Also store securely, outside the database: `MYELINATE_BOOTSTRAP_ADMIN_KEY`, the Anthropic API key, OIDC settings, and the
exact image tag or package version you run.

## PostgreSQL backup and restore

Use whatever your platform already provides. Managed Postgres (RDS, Cloud SQL, Azure Database for PostgreSQL) with
automated snapshots and point-in-time recovery is the simplest choice.

Logical backup:

```bash
pg_dump --format=custom --no-owner --dbname "$PG_URL" --file myelinate-$(date +%F).dump
```

Restore into an empty database, then point `MYELINATE_DATABASE_URL` at it:

```bash
createdb myelinate_restored
pg_restore --no-owner --dbname "postgresql://…/myelinate_restored" myelinate-2026-10-16.dump
```

`$PG_URL` is a plain `postgresql://` URL for the CLI tools. Myelinate itself needs the `postgresql+asyncpg://` form.

After a restore:

1. Start **one** gateway and check `/readyz`. Missing tables are created automatically; existing ones are left alone.
2. Jobs that were `running` at backup time are recovered when their lease expires (up to 10 minutes).
3. Note that keys revoked after the backup was taken are valid again. Re-revoke them.

Test restores regularly. A backup you haven't restored isn't a backup.

### SQLite

For dev or single-node installs, use SQLite's online backup, which is safe while the gateway runs:

```bash
sqlite3 /data/myelinate.db ".backup '/backups/myelinate-$(date +%F).db'"
```

To restore, stop the process and replace the file. Copying the file while it is being written can produce a corrupt
copy.

## Schema management: current state

!!! warning "There are no managed migrations yet"
    At startup, Myelinate calls SQLAlchemy `create_all`. That creates **missing tables** but never alters existing
    ones: it doesn't add columns, change types or add indexes to existing tables. There is no Alembic (or other)
    migration history in the project yet.

What this means in practice:

- Upgrades that add only new tables work automatically.
- An upgrade that adds or changes a column on an existing table will fail at runtime with errors such as
  `column "…" does not exist` (Postgres) or `no such column` (SQLite) until you alter the table yourself.
- The package version (`pyproject.toml`) and the API version string reported in `/openapi.json` are not tied to a
  schema version, so you can't tell from a version number whether the schema changed.

Recommendations:

- **Pin exact versions**: the image digest or tag, or `myelinate==X.Y.Z`. Never deploy `latest` to production.
- Before upgrading, compare `src/myelinate/db.py` between the two versions. If any existing table changed, write and
  test the DDL yourself.
- If you run Myelinate long-term, adopt Alembic before your first schema change. Generate a baseline from the current
  models, `alembic stamp head` on existing databases, and manage changes from there. That will also be the project's
  direction, but it doesn't exist today.
- On PostgreSQL, table creation at startup is serialised with an advisory lock, so many gateways and workers can start
  together against an empty database. SQLite has no such lock, but it only supports a single process anyway.

## Upgrade procedure

1. Read the [changelog](../changelog.md) for the target version.
2. Diff `db.py` (see above). If the schema changed, prepare and test the DDL on a staging copy of production data.
3. Take a backup (or confirm the latest PITR point).
4. Upgrade staging. Run a smoke test: auth, `POST /v1/recall`, `POST /v1/episodes`, `GET /v1/changesets`,
   `GET /v1/agents/{id}/insights`. Watch `myelinate_jobs_total{status="failed"}` for an hour.
5. Apply any DDL to production.
6. Roll the gateways, then the workers.
7. Watch error rates, recall latency and job failures. To roll back, deploy the previous version. This is only safe if
   the schema is still compatible with it, which is another reason to keep schema changes additive.

## Rolling upgrades and zero downtime

When an upgrade needs no schema change, rolling updates work:

- Gateways are stateless. With a readiness probe on `/readyz` and at least 2 replicas, a rolling update serves traffic
  throughout. Give pods a termination grace period of at least 40 s. Uvicorn drains requests, and the embedded worker
  waits up to 30 s for in-flight jobs.
- Workers can be replaced at any time. On `SIGTERM` they stop claiming and give in-flight jobs up to 30 s. Anything
  interrupted returns to the queue after its 10-minute lease and is retried (see
  [Scaling and HA](scaling-and-ha.md#job-queue)).
- During the rollout, old and new workers share one queue. If the new version introduces a new job kind, old workers
  that claim it fail it (`unknown job kind`), and the job may run out of attempts. Upgrade workers promptly, or scale
  old workers to zero first.
- Adapters fail open, so a brief gateway outage costs learned context for those runs, not agent availability.

Schema-changing upgrades need care: apply additive DDL first (new nullable columns or new tables), roll out code that
uses it, and remove old structures only in a later release.

See also: [Scaling and HA](scaling-and-ha.md), [Security](security.md), [Troubleshooting](../troubleshooting.md).
