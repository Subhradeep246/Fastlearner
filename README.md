# Zipity

> A fast, personal AI learning companion that remembers what matters and wakes with a double clap.

Zipity is a native desktop companion designed to grow with a learner from their first school years through graduation. It brings subjects, assignments, goals, context, voice, and an AI tutor into one focused workspace—without treating every conversation as a blank slate.

![Zipity icon](apps/desktop/app-icon.png)

## What Zipity can do

- **Know the learner** — first-run onboarding creates a personal profile; relevant context stays scoped to that device owner.
- **Keep school organized** — manage many subjects, assignments, and learning goals in one desktop app.
- **Explain clearly** — the assistant streams structured answers with headings, lists, tables, quotes, and code blocks.
- **Talk naturally** — optional ElevenLabs speech provides a fast voice loop.
- **Wake hands-free** — double-clap to activate Zipity, then speak a request.
- **Stay responsive** — native Rust audio handling and frame-batched answer rendering keep the experience smooth.

## How it is built

| Layer         | Technology                                 |
| ------------- | ------------------------------------------ |
| Desktop app   | Tauri 2, Rust, React 19, TypeScript, Vite  |
| Voice wake-up | Rust, CPAL, adaptive double-clap detector  |
| API           | FastAPI, Python 3.12                       |
| Data          | PostgreSQL + pgvector, Neo4j, Valkey/Redis |
| AI            | Baseten-hosted GPT-OSS 120B                |
| Speech        | ElevenLabs (optional)                      |

## Quick start

These instructions run Zipity locally on a developer machine. The desktop app is currently **Windows-first**; macOS and Linux contributors also need the normal Tauri system dependencies for their platform.

### 1. Install prerequisites

Install the following before cloning the repository:

- [Node.js 22.13.1](https://nodejs.org/) and npm 10
- [Python 3.12.9](https://www.python.org/downloads/)
- [uv](https://docs.astral.sh/uv/)
- [Rust 1.88](https://rustup.rs/)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it
- On Windows: Microsoft C++ Build Tools / Windows SDK, as required by Tauri

Confirm the versions used by this project:

```powershell
node --version
python --version
uv --version
cargo --version
docker --version
```

> The repository pins its expected versions in `.nvmrc`, `.python-version`, and `rust-toolchain.toml`.

### 2. Clone and install

```powershell
git clone https://github.com/Subhradeep246/Fastlearner.git
cd Fastlearner
npm ci
uv sync --project services/api --locked
```

### 3. Configure local environment variables

Create your private environment file:

```powershell
Copy-Item .env.example .env
```

Open `.env` and replace every value written as `<...>`. At minimum, set:

| Variable                 | What to provide                                             |
| ------------------------ | ----------------------------------------------------------- |
| `POSTGRES_PASSWORD`      | Any strong local database password                          |
| `DATABASE_URL`           | Use the same PostgreSQL password in this URL                |
| `NEO4J_PASSWORD`         | Any strong local Neo4j password                             |
| `AI_API_KEY`             | Your Baseten API key                                        |
| `APP_ENCRYPTION_KEY`     | A long random secret used to protect local application data |
| `SESSION_SIGNING_SECRET` | A separate long random secret                               |

For voice features, also set:

| Variable              | What to provide                                         |
| --------------------- | ------------------------------------------------------- |
| `ELEVENLABS_API_KEY`  | Your ElevenLabs API key                                 |
| `ELEVENLABS_VOICE_ID` | An ElevenLabs voice ID (a sensible default is included) |

`ELEVENLABS_API_KEY` is optional: the app still runs without speech synthesis. Keep all keys in `.env`; never commit that file or put secrets in a `VITE_*` variable.

### 4. Check the machine, then start Zipity

```powershell
npm run local:check
npm run dev:local
```

The supervisor starts PostgreSQL/pgvector, Neo4j, and Valkey in Docker; applies migrations; seeds the demo data; starts the worker and FastAPI service; then opens the Tauri desktop app.

On first launch, complete onboarding or use the included demo learner profile to explore the full experience.

## Using the companion

1. Create or choose a learner profile during onboarding.
2. Add subjects, assignments, and goals.
3. Ask Zipity for explanations, plans, revision help, or a breakdown of an assignment.
4. For hands-free mode, make two clear claps in sequence, wait for the wake confirmation, then speak your request.

### Double-clap troubleshooting

The detector adapts to background noise, but physical audio devices vary. If it does not wake:

- grant microphone permission to the desktop app;
- select the intended microphone as the default Windows input;
- make two sharp claps about half a second apart;
- close other apps that may be holding the microphone exclusively;
- reopen Zipity after changing an audio device.

The app remains fully usable with keyboard input if the microphone is unavailable.

## Useful commands

| Command                                            | Purpose                                                                     |
| -------------------------------------------------- | --------------------------------------------------------------------------- |
| `npm run dev:local`                                | Start databases, services, migrations, seed data, and the Tauri desktop app |
| `npm run dev:services`                             | Start only the backend services for headless development                    |
| `npm run local:check`                              | Validate configuration and local tooling without starting services          |
| `npm run api:dev`                                  | Run the FastAPI service directly for API debugging                          |
| `npm run db:migrate`                               | Apply database migrations                                                   |
| `npm run db:seed`                                  | Seed the local demo data                                                    |
| `npm run ci`                                       | Run formatting, contracts, linting, types, tests, builds, and Rust checks   |
| `npm run dev:reset -- --confirm delete-local-data` | Delete all local Zipity Docker data and start fresh                         |

> `dev:reset` is destructive: it removes the local PostgreSQL, Neo4j, and Valkey volumes. Only use it when you intentionally want to erase local development data.

## Project layout

```text
apps/
  desktop/             Tauri + React desktop experience
  web-dashboard/       Browser dashboard surface
crates/
  wake-detector/       Native Rust audio capture and double-clap detection
services/
  api/                 FastAPI API, persistence, AI and speech integrations
packages/
  contracts/           Generated API contracts
  ui/                  Shared UI components and onboarding flow
  content/             Shared learning content utilities
infra/
  docker-compose.yml   Local PostgreSQL, Neo4j, and Valkey services
docs/
  local-development.md Detailed local development notes
```

## Privacy and local data

Zipity is designed around personal context. Profile and learning context are owner-scoped, and API keys remain server-side in the local environment. Running locally still sends prompts to the AI and speech providers you configure (Baseten and optionally ElevenLabs), so use only credentials and data you are comfortable sharing with those providers.

## Contributing

Before opening a pull request, run:

```powershell
npm run ci
```

For focused work, each workspace package also exposes its own `build`, `typecheck`, and `test` scripts. See `docs/local-development.md` for service lifecycle and recovery guidance.

---

Built with Codex, GPT-5.6, Tauri, FastAPI, Baseten, and ElevenLabs.
