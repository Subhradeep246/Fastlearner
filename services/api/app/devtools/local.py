from __future__ import annotations

import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[4]
API_ROOT = ROOT / "services" / "api"
ENV_FILE = ROOT / ".env"
COMPOSE_FILE = ROOT / "infra" / "docker-compose.yml"
SERVICES = ("postgres", "neo4j", "redis")
STARTUP_ORDER = ("dependencies", "migration", "seed", "worker", "API", "desktop")
RESET_CONFIRMATION = "delete-local-data"


def startup_order(*, services_only: bool) -> tuple[str, ...]:
    return STARTUP_ORDER[:-1] if services_only else STARTUP_ORDER


@dataclass(frozen=True)
class SafeFailure:
    component: str
    affected_feature: str
    outcome: str
    remediation: str

    def render(self) -> str:
        return (
            f"[failed] {self.component}: {self.outcome}\n"
            f"  affected feature: {self.affected_feature}\n"
            f"  remediation: {self.remediation}"
        )


class LocalDevError(RuntimeError):
    def __init__(self, failure: SafeFailure) -> None:
        self.failure = failure
        super().__init__(failure.render())


def read_dotenv(path: Path = ENV_FILE) -> dict[str, str]:
    if not path.is_file():
        raise LocalDevError(
            SafeFailure(
                ".env",
                "local services and application startup",
                "local configuration file is unavailable",
                "copy .env.example to .env and replace every <...> placeholder",
            )
        )
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        values[name.strip()] = value.strip().strip('"').strip("'")
    return values


def local_environment(path: Path = ENV_FILE) -> dict[str, str]:
    values = read_dotenv(path) if path.is_file() else {}
    required = (
        "POSTGRES_PASSWORD",
        "NEO4J_PASSWORD",
        "DATABASE_URL",
        "REDIS_URL",
        "NEO4J_URI",
    )
    for name in set(values).union(required):
        if name in os.environ:
            values[name] = os.environ[name]
    if not path.is_file() and not all(values.get(name) for name in required):
        raise LocalDevError(
            SafeFailure(
                ".env",
                "local services and application startup",
                "local configuration file is unavailable",
                "copy .env.example to .env and replace every <...> placeholder",
            )
        )
    missing = [
        name
        for name in required
        if not values.get(name) or _is_placeholder(values[name])
    ]
    if missing:
        names = ", ".join(sorted(missing))
        raise LocalDevError(
            SafeFailure(
                "configuration",
                "database, graph memory, and background jobs",
                f"required local setting(s) are missing: {names}",
                "replace the named placeholders in .env; configured values are never printed",
            )
        )
    return values


def _is_placeholder(value: str) -> bool:
    stripped = value.strip()
    return stripped.startswith("<") and stripped.endswith(">")


def require_tool(name: str, feature: str, remediation: str) -> None:
    if shutil.which(name) is None:
        raise LocalDevError(
            SafeFailure(name, feature, "required executable is unavailable", remediation)
        )


def require_artifact(path: Path, component: str, feature: str, remediation: str) -> None:
    if not path.exists():
        raise LocalDevError(
            SafeFailure(component, feature, "required implementation is unavailable", remediation)
        )


def migration_command() -> list[str]:
    return ["uv", "run", "--project", str(API_ROOT), "python", "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"]


def seed_command() -> list[str]:
    return ["uv", "run", "--project", str(API_ROOT), "python", "-m", "app.commands.seed", "--profile", "local"]


def worker_command() -> list[str]:
    return ["uv", "run", "--project", str(API_ROOT), "python", "-m", "app.workers.main"]


def api_command(environment: dict[str, str]) -> list[str]:
    host = environment.get("API_HOST", "127.0.0.1")
    port = _api_port(environment.get("API_PUBLIC_URL", "http://localhost:8000/v1"))
    return ["uv", "run", "--project", str(API_ROOT), "uvicorn", "app.main:app", "--host", host, "--port", str(port), "--reload"]


def desktop_command() -> list[str]:
    return ["npm", "run", "tauri", "--workspace", "@fastlearner/desktop", "--", "dev"]


def _api_port(url: str) -> int:
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).port or 8000
    except ValueError:
        return 8000


def compose_command(*arguments: str) -> list[str]:
    return [
        "docker",
        "compose",
        "--env-file",
        str(ENV_FILE),
        "-f",
        str(COMPOSE_FILE),
        *arguments,
    ]


def run_checked(command: Sequence[str], component: str, feature: str, remediation: str, *, cwd: Path = ROOT, capture: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            cwd=cwd,
            check=True,
            text=True,
            capture_output=capture,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise LocalDevError(SafeFailure(component, feature, "command failed", remediation)) from error


def validate_startup_artifacts(*, desktop: bool) -> None:
    require_artifact(API_ROOT / "alembic.ini", "migration", "relational persistence", "complete the migration foundation and verify services/api/alembic.ini exists")
    require_artifact(API_ROOT / "app" / "commands" / "seed.py", "seed", "local development personas", "complete the seed command and verify app.commands.seed is importable")
    require_artifact(API_ROOT / "app" / "workers" / "main.py", "worker", "background jobs and synchronization", "complete the worker primitive and verify app.workers.main is importable")
    if desktop:
        require_artifact(ROOT / "apps" / "desktop" / "src-tauri" / "tauri.conf.json", "desktop", "desktop companion", "restore the Tauri desktop configuration")


def preflight(*, desktop: bool, artifacts: bool = True) -> dict[str, str]:
    environment = local_environment()
    require_tool("docker", "PostgreSQL, graph memory, and background job storage", "install Docker Desktop/Engine and ensure docker is on PATH")
    require_tool("uv", "migration, seed, API, and worker", "install uv and ensure uv is on PATH")
    if desktop:
        require_tool("npm", "desktop companion", "install the Node.js version from .nvmrc and run npm ci")
    run_checked(["docker", "compose", "version"], "Docker Compose", "local dependency orchestration", "install the Docker Compose v2 plugin")
    if artifacts:
        validate_startup_artifacts(desktop=desktop)
    print("[ready] preflight: configuration names, tools, and startup artifacts validated")
    return environment


def running_services() -> set[str]:
    result = run_checked(
        compose_command("ps", "--services", "--filter", "status=running"),
        "Docker Compose",
        "local dependencies",
        "start Docker and run npm run dev:services again",
        capture=True,
    )
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def start_dependencies() -> set[str]:
    before = running_services()
    run_checked(
        compose_command("up", "-d", "--wait", *SERVICES),
        "local dependencies",
        "relational store, graph store, and job store",
        "inspect docker compose logs for the named service, correct .env, then retry",
    )
    started = set(SERVICES) - before
    for service in SERVICES:
        print(f"[ready] {service}: healthy")
    return started


def migrate() -> None:
    require_artifact(API_ROOT / "alembic.ini", "migration", "relational persistence", "complete the migration foundation and verify services/api/alembic.ini exists")
    run_checked(migration_command(), "migration", "relational persistence", "inspect the migration revision chain and database availability", cwd=API_ROOT)
    print("[ready] migration: database is at the current revision")


def seed() -> None:
    require_artifact(API_ROOT / "app" / "commands" / "seed.py", "seed", "local development personas", "complete the seed command and verify app.commands.seed is importable")
    run_checked(seed_command(), "seed", "local development personas", "inspect the idempotent local seed command and database availability", cwd=API_ROOT)
    print("[ready] seed: local personas and versioned data are current")


def spawn(command: Sequence[str], component: str) -> subprocess.Popen[str]:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            list(command),
            cwd=ROOT,
            text=True,
            creationflags=flags,
            start_new_session=os.name != "nt",
        )
    except OSError as error:
        raise LocalDevError(
            SafeFailure(component, component, "process could not start", f"run the corresponding npm script directly to inspect {component}")
        ) from error
    return process


def wait_for_api(process: subprocess.Popen[str], environment: dict[str, str], timeout_seconds: float = 45.0) -> None:
    base = environment.get("API_PUBLIC_URL", "http://localhost:8000/v1").rstrip("/")
    health_url = f"{base}/health"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise LocalDevError(SafeFailure("API", "all client features", "API exited before readiness", "run npm run api:dev to inspect the API failure"))
        try:
            with urllib.request.urlopen(health_url, timeout=1.0) as response:  # noqa: S310 - configured local URL
                if response.status == 200:
                    print(f"[ready] API: {health_url}")
                    return
        except (urllib.error.URLError, TimeoutError):
            time.sleep(0.25)
    raise LocalDevError(SafeFailure("API", "desktop and web API features", "health check timed out", "verify port 8000 is available and inspect API output"))


def ensure_alive(process: subprocess.Popen[str], component: str, delay: float = 0.75) -> None:
    time.sleep(delay)
    if process.poll() is not None:
        raise LocalDevError(SafeFailure(component, component, "process exited before readiness", f"run npm run {component} directly to inspect the failure"))
    print(f"[ready] {component}: process is running")


def terminate_process(process: subprocess.Popen[str], name: str) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.kill(-process.pid, signal.SIGTERM)
        process.wait(timeout=8)
    except (OSError, subprocess.TimeoutExpired):
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    print(f"[stopped] {name}")


def supervise(*, services_only: bool) -> None:
    environment = preflight(desktop=not services_only)
    started_services: set[str] = set()
    processes: list[tuple[str, subprocess.Popen[str]]] = []
    try:
        started_services = start_dependencies()
        migrate()
        seed()
        worker = spawn(worker_command(), "worker")
        processes.append(("worker", worker))
        api = spawn(api_command(environment), "API")
        processes.append(("API", api))
        ensure_alive(worker, "worker")
        wait_for_api(api, environment)
        if environment.get("AI_PROVIDER", "openai").lower() == "disabled":
            print("[degraded] AI features: AI_PROVIDER is disabled; deterministic local features remain available")
        if not services_only:
            desktop = spawn(desktop_command(), "desktop")
            processes.append(("desktop", desktop))
            ensure_alive(desktop, "desktop")
        profile = "services-only" if services_only else "desktop"
        print(f"[ready] local environment: {profile} profile; press Ctrl+C to stop")
        while True:
            for name, process in processes:
                return_code = process.poll()
                if return_code is not None:
                    raise LocalDevError(SafeFailure(name, profile, f"process exited with status {return_code}", f"run the {name} command directly to inspect its output"))
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n[stopping] coordinated local shutdown requested")
    finally:
        for name, process in reversed(processes):
            terminate_process(process, name)
        if started_services:
            run_checked(
                compose_command("stop", *sorted(started_services)),
                "Docker Compose",
                "local dependency shutdown",
                "run docker compose -f infra/docker-compose.yml stop; named volumes remain intact",
            )
            print("[stopped] dependencies started by this supervisor; named data volumes were preserved")


def reset_local_data(confirmation: str | None) -> None:
    if confirmation != RESET_CONFIRMATION:
        raise LocalDevError(
            SafeFailure(
                "destructive reset",
                "all local PostgreSQL, Neo4j, and job-store data",
                "confirmation was not supplied; no data was changed",
                f"run npm run dev:reset -- --confirm {RESET_CONFIRMATION}",
            )
        )
    preflight(desktop=False, artifacts=False)
    run_checked(
        compose_command("down", "--volumes", "--remove-orphans"),
        "destructive reset",
        "all local service data",
        "ensure Docker is running, then retry the explicitly confirmed reset",
    )
    print("[reset] local containers and named data volumes were removed")


def run_worker() -> None:
    preflight(desktop=False, artifacts=False)
    require_artifact(
        API_ROOT / "app" / "workers" / "main.py",
        "worker",
        "background jobs and synchronization",
        "complete the worker primitive and verify app.workers.main is importable",
    )
    process = spawn(worker_command(), "worker")
    try:
        ensure_alive(process, "worker")
        process.wait()
    except KeyboardInterrupt:
        print("\n[stopping] worker shutdown requested")
    finally:
        terminate_process(process, "worker")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FastLearner cross-platform local development supervisor")
    subcommands = parser.add_subparsers(dest="command", required=True)
    start = subcommands.add_parser("start", help="start dependencies, migrate, seed, API, worker, and optionally desktop")
    start.add_argument("--services-only", action="store_true", help="run the headless API/worker profile without Tauri")
    reset = subcommands.add_parser("reset", help="destructively remove local service containers and named volumes")
    reset.add_argument("--confirm", metavar="PHRASE")
    subcommands.add_parser("migrate", help="upgrade the local database to the current revision")
    subcommands.add_parser("seed", help="apply idempotent local development seeds")
    subcommands.add_parser("worker", help="run the durable background worker")
    check = subcommands.add_parser("check", help="validate local configuration, tools, and startup artifacts")
    check.add_argument("--desktop", action="store_true", help="also validate desktop startup dependencies")
    return parser


def main(arguments: Sequence[str] | None = None) -> int:
    options = build_parser().parse_args(arguments)
    try:
        if options.command == "start":
            supervise(services_only=options.services_only)
        elif options.command == "reset":
            reset_local_data(options.confirm)
        elif options.command == "migrate":
            preflight(desktop=False, artifacts=False)
            migrate()
        elif options.command == "seed":
            preflight(desktop=False, artifacts=False)
            seed()
        elif options.command == "worker":
            run_worker()
        elif options.command == "check":
            preflight(desktop=options.desktop)
    except LocalDevError as error:
        print(error.failure.render(), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
