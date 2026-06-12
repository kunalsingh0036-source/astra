"""
Service manager — starts, stops, and monitors agent backend processes.

Each agent is defined as a ServiceConfig with:
- The command to start it
- The working directory
- The port it runs on
- Health check URL

The manager tracks PIDs, checks health, and provides a unified
interface for Astra to control its entire fleet from conversation.

Port assignments (fixed, no conflicts):
    Bookkeeper:     8000  (Django)
    Apex Outreach:  8001  (FastAPI)
    LinkedIn:       8002  (FastAPI)
    HelmTech:       8003  (FastAPI)
    Finance:        8004  (FastAPI)
    Email:          8005  (FastAPI)
    WhatsApp GW:    8600  (FastAPI)
    A2A Bridge:     8500  (FastAPI — proxies to all above)
"""

import asyncio
import json
import logging
import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Where we persist PIDs across restarts
PID_DIR = Path(__file__).parent.parent.parent / ".pids"
LOG_DIR = Path(__file__).parent.parent.parent / ".logs"


@dataclass
class ServiceConfig:
    """Configuration for an agent backend service."""

    name: str
    display_name: str
    port: int
    working_dir: str
    start_command: list[str]
    health_url: str
    venv_path: str | None = None  # Path to .venv/bin/activate
    env_extras: dict[str, str] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # Docker services needed
    startup_wait: float = 3.0  # Seconds to wait after starting before health check


# ---------------------------------------------------------------------------
# Service definitions — one per agent
# ---------------------------------------------------------------------------

PROJECTS_DIR = "/Users/kunalsingh/Claude Code"

SERVICES: dict[str, ServiceConfig] = {
    "bookkeeper": ServiceConfig(
        name="bookkeeper",
        display_name="Bookkeeper Agent",
        port=8000,
        working_dir=f"{PROJECTS_DIR}/bookkeeper-agent",
        start_command=["python", "manage.py", "runserver", "0.0.0.0:8000"],
        health_url="http://localhost:8000/admin/login/",
        venv_path=f"{PROJECTS_DIR}/bookkeeper-agent/.venv/bin/activate",
        startup_wait=4.0,
    ),
    "apex": ServiceConfig(
        name="apex",
        display_name="Apex Outreach Agent",
        port=8001,
        working_dir=f"{PROJECTS_DIR}/apex-sales-team/backend",
        start_command=[
            "uvicorn", "app.main:app",
            "--host", "0.0.0.0",
            "--port", "8001",
            "--reload",
        ],
        health_url="http://localhost:8001/docs",
        venv_path=f"{PROJECTS_DIR}/apex-sales-team/backend/.venv/bin/activate",
        startup_wait=5.0,
    ),
    "linkedin": ServiceConfig(
        name="linkedin",
        display_name="LinkedIn Agent",
        port=8002,
        working_dir=f"{PROJECTS_DIR}/linkedin-agent/backend",
        start_command=[
            "uvicorn", "main:app",
            "--host", "0.0.0.0",
            "--port", "8002",
        ],
        health_url="http://localhost:8002/docs",
        venv_path=f"{PROJECTS_DIR}/linkedin-agent/backend/.venv/bin/activate",
        startup_wait=4.0,
    ),
    "helmtech": ServiceConfig(
        name="helmtech",
        display_name="HelmTech Outreach Agent",
        port=8003,
        working_dir=f"{PROJECTS_DIR}/helmtech-outreach-agent/backend",
        start_command=[
            "uvicorn", "app.main:app",
            "--host", "0.0.0.0",
            "--port", "8003",
        ],
        health_url="http://localhost:8003/docs",
        venv_path=f"{PROJECTS_DIR}/helmtech-outreach-agent/backend/.venv/bin/activate",
        startup_wait=4.0,
    ),
    "whatsapp": ServiceConfig(
        name="whatsapp",
        display_name="WhatsApp Gateway",
        port=8600,
        working_dir=f"{PROJECTS_DIR}/whatsapp-gateway",
        start_command=[
            "uvicorn", "gateway.main:app",
            "--host", "0.0.0.0",
            "--port", "8600",
        ],
        health_url="http://localhost:8600/health",
        venv_path=f"{PROJECTS_DIR}/whatsapp-gateway/.venv/bin/activate",
        startup_wait=4.0,
    ),
    "bridge": ServiceConfig(
        name="bridge",
        display_name="A2A Bridge Server",
        port=8500,
        working_dir=f"{PROJECTS_DIR}/astra",
        start_command=[
            "python", "-m", "astra.agents.external.bridge_server",
        ],
        health_url="http://localhost:8500/health",
        venv_path=f"{PROJECTS_DIR}/astra/.venv/bin/activate",
        startup_wait=2.0,
    ),
    # "finance": ServiceConfig(  # DISABLED: directory does not exist
    #     ...
    # ),
    # "email": ServiceConfig(  # DISABLED: directory does not exist
    #     ...
    # ),
    "scheduler": ServiceConfig(
        name="scheduler",
        display_name="Astra Scheduler (APScheduler)",
        port=0,  # No HTTP port — in-process async scheduler
        working_dir=f"{PROJECTS_DIR}/astra",
        start_command=[
            "python", "-m", "astra.scheduler.app",
        ],
        health_url="",  # Liveness via pidfile; jobs log on each fire
        venv_path=f"{PROJECTS_DIR}/astra/.venv/bin/activate",
        startup_wait=3.0,
    ),
}


class ServiceManager:
    """Manages lifecycle of all agent backend services.

    Tracks running processes, checks health, handles startup/shutdown.
    PID files are persisted to .pids/ so services survive Astra restarts.
    """

    def __init__(self):
        self._processes: dict[str, subprocess.Popen] = {}
        PID_DIR.mkdir(exist_ok=True)
        LOG_DIR.mkdir(exist_ok=True)
        # Recover any previously running services
        self._recover_pids()

    def _recover_pids(self) -> None:
        """Check for PID files from a previous session and verify they're alive."""
        for pid_file in PID_DIR.glob("*.pid"):
            name = pid_file.stem
            try:
                pid = int(pid_file.read_text().strip())
                # Check if process is still alive
                os.kill(pid, 0)  # Signal 0 = just check existence
                logger.info(f"Recovered running service '{name}' (PID {pid})")
            except (ProcessLookupError, ValueError, PermissionError):
                # Process is dead, clean up PID file
                pid_file.unlink(missing_ok=True)
                logger.debug(f"Cleaned stale PID file for '{name}'")

    def _save_pid(self, name: str, pid: int) -> None:
        """Persist a PID to disk."""
        (PID_DIR / f"{name}.pid").write_text(str(pid))

    def _clear_pid(self, name: str) -> None:
        """Remove a PID file."""
        (PID_DIR / f"{name}.pid").unlink(missing_ok=True)

    # The bash `astra up` controller writes pidfiles here. Keeping this
    # as a list so both sources are consulted (our own .pids/ for things
    # ServiceManager spawned, astra-control/pids/ for things the user
    # launched via `astra up`). First hit wins.
    _EXTERNAL_PID_DIRS: tuple[Path, ...] = (
        Path("/Users/kunalsingh/Claude Code/astra-control/pids"),
    )

    def _get_saved_pid(self, name: str) -> int | None:
        """Read a saved PID from disk, checking the astra-control pid
        store as well so services started via `astra up` are detected.

        Falls through to a live-port probe as a last resort — a service
        bound to its configured port is running regardless of whether
        we have a pidfile for it.
        """
        candidates = [PID_DIR / f"{name}.pid"]
        for extra in self._EXTERNAL_PID_DIRS:
            candidates.append(extra / f"{name}.pid")

        for pid_file in candidates:
            if not pid_file.exists():
                continue
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # Check alive
                return pid
            except (ProcessLookupError, ValueError, PermissionError):
                # Only clean up pidfiles we own
                if pid_file.parent == PID_DIR:
                    pid_file.unlink(missing_ok=True)

        # Last-resort: if the configured port is bound, the service is
        # up even if nobody wrote a pidfile we can see. We don't know
        # the pid, so return a sentinel (-1) which health_check treats
        # as "alive, unknown pid".
        config = SERVICES.get(name)
        if config and config.port and self._is_port_in_use(config.port):
            return -1
        return None

    def _is_port_in_use(self, port: int) -> bool:
        """Check if a port is already bound."""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(("localhost", port)) == 0

    def start(self, name: str) -> dict:
        """Start a service by name.

        Returns dict with status, PID, and any errors.
        """
        if name not in SERVICES:
            return {"status": "error", "message": f"Unknown service: {name}"}

        config = SERVICES[name]

        # Check if already running
        existing_pid = self._get_saved_pid(name)
        if existing_pid:
            return {
                "status": "already_running",
                "service": config.display_name,
                "pid": existing_pid,
                "port": config.port,
            }

        # Check port conflict
        if self._is_port_in_use(config.port):
            return {
                "status": "error",
                "message": f"Port {config.port} is already in use",
                "service": config.display_name,
            }

        # Check working directory exists
        if not Path(config.working_dir).exists():
            return {
                "status": "error",
                "message": f"Working directory not found: {config.working_dir}",
                "service": config.display_name,
            }

        # Build the command — use venv's python directly instead of shell source
        # (shell source doesn't work reliably in subprocess.Popen)
        if config.venv_path and Path(config.venv_path).exists():
            venv_bin = Path(config.venv_path).parent  # .venv/bin/activate → .venv/bin/
            # Replace 'python' and 'uvicorn' with absolute paths
            cmd = []
            for part in config.start_command:
                if part in ('python', 'python3', 'uvicorn'):
                    cmd.append(str(venv_bin / part))
                else:
                    cmd.append(part)
            shell_cmd = " ".join(cmd)
        else:
            shell_cmd = " ".join(config.start_command)

        # Open log files
        log_out = open(LOG_DIR / f"{name}.log", "a")
        log_err = open(LOG_DIR / f"{name}.err", "a")

        # Write startup marker
        timestamp = datetime.now(timezone.utc).isoformat()
        log_out.write(f"\n--- Service started at {timestamp} ---\n")
        log_out.flush()

        try:
            # Merge environment — strip Astra's own DB/Redis URLs to prevent
            # them from bleeding into child agent processes that read DATABASE_URL
            env = os.environ.copy()
            astra_env_keys = ["DATABASE_URL", "REDIS_URL"]
            for key in astra_env_keys:
                env.pop(key, None)
            env.update(config.env_extras)

            process = subprocess.Popen(
                shell_cmd,
                shell=True,
                cwd=config.working_dir,
                stdout=log_out,
                stderr=log_err,
                env=env,
                preexec_fn=os.setsid,  # New process group for clean shutdown
            )

            self._processes[name] = process
            self._save_pid(name, process.pid)

            logger.info(
                f"Started '{config.display_name}' (PID {process.pid}) "
                f"on port {config.port}"
            )

            return {
                "status": "started",
                "service": config.display_name,
                "pid": process.pid,
                "port": config.port,
                "log": str(LOG_DIR / f"{name}.log"),
            }

        except Exception as e:
            log_out.close()
            log_err.close()
            return {
                "status": "error",
                "service": config.display_name,
                "message": str(e),
            }

    def stop(self, name: str) -> dict:
        """Stop a running service."""
        if name not in SERVICES:
            return {"status": "error", "message": f"Unknown service: {name}"}

        config = SERVICES[name]
        pid = self._get_saved_pid(name)

        if not pid:
            # Also check our in-memory processes
            proc = self._processes.get(name)
            if proc and proc.poll() is None:
                pid = proc.pid
            else:
                return {
                    "status": "not_running",
                    "service": config.display_name,
                }

        try:
            # Kill the whole process group (catches child processes)
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            # Give it a moment to shut down gracefully
            time.sleep(1)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # Already dead

        except ProcessLookupError:
            pass  # Already dead
        except PermissionError:
            # Try regular kill
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        self._clear_pid(name)
        self._processes.pop(name, None)

        logger.info(f"Stopped '{config.display_name}' (PID {pid})")

        return {
            "status": "stopped",
            "service": config.display_name,
            "pid": pid,
        }

    def start_all(self) -> list[dict]:
        """Start all services (bridge last, since it depends on the others)."""
        results = []
        # Start agents first, bridge last
        agent_names = [n for n in SERVICES if n != "bridge"]
        for name in agent_names:
            results.append(self.start(name))

        # Brief pause for agents to initialize
        time.sleep(1)

        # Start bridge
        results.append(self.start("bridge"))
        return results

    def stop_all(self) -> list[dict]:
        """Stop all services (bridge first, agents after)."""
        results = []
        # Stop bridge first
        results.append(self.stop("bridge"))

        # Stop agents
        for name in SERVICES:
            if name != "bridge":
                results.append(self.stop(name))

        return results

    async def health_check(self, name: str) -> dict:
        """Check health of a single service."""
        if name not in SERVICES:
            return {"service": name, "status": "unknown"}

        config = SERVICES[name]
        pid = self._get_saved_pid(name)

        if not pid:
            return {
                "service": config.display_name,
                "status": "stopped",
                "port": config.port,
            }

        # Scheduler has no HTTP endpoint — liveness is pid-only.
        if not config.health_url:
            return {
                "service": config.display_name,
                "status": "healthy",
                "pid": pid if pid > 0 else None,
                "port": config.port,
            }

        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(config.health_url)
                healthy = response.status_code < 500
        except Exception:
            healthy = False

        return {
            "service": config.display_name,
            "status": "healthy" if healthy else "unhealthy",
            "pid": pid if pid > 0 else None,
            "port": config.port,
        }

    async def health_check_all(self) -> list[dict]:
        """Health check all services concurrently."""
        tasks = [self.health_check(name) for name in SERVICES]
        return await asyncio.gather(*tasks)

    def status_all(self) -> list[dict]:
        """Quick status of all services.

        Uses `_get_saved_pid` which consults our own .pids/, the
        astra-control pid store (where `astra up` writes), AND falls
        back to a live TCP probe. So a service started by any of the
        three paths shows as running.
        """
        results = []
        for name, config in SERVICES.items():
            pid = self._get_saved_pid(name)
            results.append({
                "service": config.display_name,
                "name": name,
                "status": "running" if pid else "stopped",
                "pid": pid if (pid is not None and pid > 0) else None,
                "port": config.port,
            })
        return results

    def get_logs(self, name: str, lines: int = 50) -> str:
        """Get recent log output for a service."""
        log_file = LOG_DIR / f"{name}.log"
        err_file = LOG_DIR / f"{name}.err"

        output = []
        for f, label in [(log_file, "STDOUT"), (err_file, "STDERR")]:
            if f.exists():
                content = f.read_text()
                tail = "\n".join(content.splitlines()[-lines:])
                if tail.strip():
                    output.append(f"--- {label} (last {lines} lines) ---\n{tail}")

        return "\n\n".join(output) if output else f"No logs found for '{name}'"


# Global singleton
service_manager = ServiceManager()
