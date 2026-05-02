"""
Webhook tunnel manager — exposes local services to the internet.

Supports two providers:
- ngrok (default): Zero-config, works instantly. Free tier gives one static domain.
- cloudflared: Free, requires Cloudflare account + domain.

Used primarily for the WhatsApp Gateway webhook (Meta needs to POST to a public URL).

Usage via Astra's MCP tools:
    start_tunnel → starts ngrok/cloudflared, returns public URL
    tunnel_status → checks if running, returns URL
    stop_tunnel → kills the tunnel process
"""

import logging
import os
import signal
import subprocess
import time
from pathlib import Path

import httpx

from astra.config import settings

logger = logging.getLogger(__name__)

PID_DIR = Path(__file__).parent.parent.parent / ".pids"
LOG_DIR = Path(__file__).parent.parent.parent / ".logs"


class TunnelManager:
    """Manages a webhook tunnel (ngrok or cloudflared) as a subprocess."""

    def __init__(self):
        self._process: subprocess.Popen | None = None
        self._public_url: str | None = None
        PID_DIR.mkdir(exist_ok=True)
        LOG_DIR.mkdir(exist_ok=True)

    def _get_saved_pid(self) -> int | None:
        """Read saved tunnel PID."""
        pid_file = PID_DIR / "tunnel.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                os.kill(pid, 0)  # Check alive
                return pid
            except (ProcessLookupError, ValueError, PermissionError):
                pid_file.unlink(missing_ok=True)
        return None

    def _save_pid(self, pid: int) -> None:
        (PID_DIR / "tunnel.pid").write_text(str(pid))

    def _clear_pid(self) -> None:
        (PID_DIR / "tunnel.pid").unlink(missing_ok=True)

    def is_running(self) -> bool:
        """Check if a tunnel process is alive."""
        return self._get_saved_pid() is not None

    def start(self, port: int = 8600) -> dict:
        """Start a tunnel to the specified local port.

        Returns dict with status, public_url, and provider info.
        """
        if self.is_running():
            url = self.get_public_url()
            return {
                "status": "already_running",
                "public_url": url,
                "provider": settings.tunnel_provider,
            }

        provider = settings.tunnel_provider

        if provider == "ngrok":
            return self._start_ngrok(port)
        elif provider == "cloudflared":
            return self._start_cloudflared(port)
        else:
            return {"status": "error", "message": f"Unknown provider: {provider}"}

    def _start_ngrok(self, port: int) -> dict:
        """Start ngrok tunnel."""
        cmd = ["ngrok", "http", str(port), "--log=stdout", "--log-format=json"]

        # Add auth token if configured (enables static domains on free tier)
        if settings.ngrok_authtoken:
            cmd.extend(["--authtoken", settings.ngrok_authtoken])

        log_file = open(LOG_DIR / "tunnel.log", "a")
        log_file.write(f"\n--- ngrok started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.flush()

        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            self._process = process
            self._save_pid(process.pid)

            # Wait for ngrok to start and expose its local API
            time.sleep(3)

            # Query ngrok's local API for the public URL
            url = self._query_ngrok_url()
            self._public_url = url

            if url:
                logger.info(f"ngrok tunnel started: {url} → localhost:{port}")
                return {
                    "status": "started",
                    "public_url": url,
                    "provider": "ngrok",
                    "pid": process.pid,
                    "webhook_url": f"{url}/api/v1/webhook",
                }
            else:
                logger.warning("ngrok started but could not determine public URL")
                return {
                    "status": "started",
                    "public_url": None,
                    "provider": "ngrok",
                    "pid": process.pid,
                    "message": "Check http://localhost:4040 for the URL",
                }

        except FileNotFoundError:
            return {
                "status": "error",
                "message": (
                    "ngrok not found. Install it:\n"
                    "  Download from https://ngrok.com/download\n"
                    "  Or: pip install pyngrok"
                ),
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _start_cloudflared(self, port: int) -> dict:
        """Start cloudflared tunnel."""
        if not settings.tunnel_hostname:
            # Quick tunnel (no custom domain — generates random URL)
            cmd = ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"]
        else:
            # Named tunnel with custom domain
            cmd = [
                "cloudflared", "tunnel", "run",
                "--url", f"http://localhost:{port}",
                settings.tunnel_hostname,
            ]

        log_file = open(LOG_DIR / "tunnel.log", "a")
        log_file.write(f"\n--- cloudflared started at {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        log_file.flush()

        try:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                preexec_fn=os.setsid,
            )
            self._process = process
            self._save_pid(process.pid)

            time.sleep(4)

            url = settings.tunnel_hostname or "Check logs for URL"
            self._public_url = url if settings.tunnel_hostname else None

            logger.info(f"cloudflared tunnel started → localhost:{port}")
            return {
                "status": "started",
                "public_url": url,
                "provider": "cloudflared",
                "pid": process.pid,
            }

        except FileNotFoundError:
            return {
                "status": "error",
                "message": "cloudflared not found. Download from https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _query_ngrok_url(self) -> str | None:
        """Query ngrok's local API for the current public URL."""
        try:
            resp = httpx.get("http://localhost:4040/api/tunnels", timeout=5.0)
            if resp.status_code == 200:
                tunnels = resp.json().get("tunnels", [])
                for tunnel in tunnels:
                    url = tunnel.get("public_url", "")
                    if url.startswith("https://"):
                        return url
                # Fallback to any URL
                if tunnels:
                    return tunnels[0].get("public_url")
        except Exception as e:
            logger.debug(f"Could not query ngrok API: {e}")
        return None

    def get_public_url(self) -> str | None:
        """Get the current public tunnel URL."""
        if not self.is_running():
            return None

        if settings.tunnel_provider == "ngrok":
            url = self._query_ngrok_url()
            self._public_url = url
            return url

        # For cloudflared with a custom domain, return the configured hostname
        if settings.tunnel_hostname:
            return f"https://{settings.tunnel_hostname}"

        return self._public_url

    def stop(self) -> dict:
        """Stop the tunnel."""
        pid = self._get_saved_pid()
        if not pid:
            return {"status": "not_running"}

        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(1)
            try:
                os.killpg(os.getpgid(pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        except PermissionError:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

        self._clear_pid()
        self._process = None
        self._public_url = None

        logger.info(f"Tunnel stopped (was PID {pid})")
        return {"status": "stopped", "pid": pid}

    def status(self) -> dict:
        """Get current tunnel status."""
        pid = self._get_saved_pid()
        if not pid:
            return {
                "status": "stopped",
                "provider": settings.tunnel_provider,
            }

        url = self.get_public_url()
        return {
            "status": "running",
            "provider": settings.tunnel_provider,
            "pid": pid,
            "public_url": url,
            "webhook_url": f"{url}/api/v1/webhook" if url else None,
        }


# Global singleton
tunnel_manager = TunnelManager()
