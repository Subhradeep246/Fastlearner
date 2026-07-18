# Local development

Copy `.env.example` to `.env` and replace every `<...>` placeholder. The supervisor reports setting names but never prints configured values.

## Commands

- `npm run dev:local` starts or connects PostgreSQL/pgvector, Neo4j, and Valkey; waits for health; migrates; seeds; starts the worker and API; waits for API readiness; then starts Tauri.
- `npm run dev:services` runs the same ordered startup without Tauri for headless development.
- `npm run db:migrate` applies the migration chain.
- `npm run db:seed` applies idempotent local persona and versioned data seeds.
- `npm run api:dev` and `npm run worker` run an individual long-lived process for diagnosis.
- `npm run local:check` validates local configuration names, tools, and startup artifacts without starting services.
- `npm run test:services` migrates, seeds, and runs Python tests against already-running local services.

Press Ctrl+C once to stop supervised processes. Services started by the supervisor are stopped in reverse lifecycle order; services that were already running are left running. Named volumes and local data are preserved.

## Destructive reset

Reset is intentionally separate and requires an exact confirmation phrase:

```text
npm run dev:reset -- --confirm delete-local-data
```

This removes the FastLearner Compose containers and named PostgreSQL, Neo4j, and Valkey volumes. Omitting or changing the phrase exits without modifying data.

## Safe remediation

Startup failures identify the unavailable component, affected feature, and a remediation command. For container diagnostics, inspect the named service locally with `docker compose --env-file .env -f infra/docker-compose.yml logs <service>`; do not paste logs containing secrets into issues.
