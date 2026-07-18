# FastLearner

FastLearner is a local-first adaptive learning desktop application. This repository is an npm, Python (`uv`), and Rust (`cargo`) workspace.

## Prerequisites

Use the pinned versions in `.nvmrc`, `.python-version`, `rust-toolchain.toml`, and the root `packageManager` field.

## One-shot checks

- `npm ci` installs JavaScript dependencies from the lockfile.
- `uv sync --project services/api --locked` installs Python dependencies.
- `npm run contracts:generate` exports FastAPI OpenAPI and regenerates TypeScript contracts.
- `npm run ci` runs all non-watch lint, type, test, build, contract, and Rust checks.

Package-specific commands are documented by each package's `package.json` or `pyproject.toml`.
