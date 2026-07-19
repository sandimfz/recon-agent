import contextlib
import os
import secrets
import socket
import time
from pathlib import Path
from typing import cast

import docker
import httpx
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from strix.config import Config

from . import SandboxInitializationError
from .runtime import AbstractRuntime, SandboxInfo


HOST_GATEWAY_HOSTNAME = "host.docker.internal"
DOCKER_TIMEOUT = 60
CONTAINER_TOOL_SERVER_PORT = 48081
CONTAINER_CAIDO_PORT = 48080


class DockerRuntime(AbstractRuntime):
    def __init__(self) -> None:
        # Propagation of DOCKER_HOST from config
        config_docker_host = Config.get("docker_host")
        if config_docker_host and not os.getenv("DOCKER_HOST"):
            os.environ["DOCKER_HOST"] = config_docker_host

        try:
            self.client = docker.from_env(timeout=DOCKER_TIMEOUT)
        except (DockerException, RequestsConnectionError, RequestsTimeout) as e:
            raise SandboxInitializationError(
                "Docker is not available",
                "Please ensure Docker Desktop is installed and running.",
            ) from e

        self._scan_container: Container | None = None
        self._tool_server_port: int | None = None
        self._tool_server_token: str | None = None
        self._caido_port: int | None = None

        # Warm sandbox: reuse one long-lived container across scans instead of
        # creating one per scan_id. Bootstrap (Caido + tool server) runs once;
        # per-scan isolation is provided by the reset sequence in create_sandbox.
        self._warm = str(Config.get("strix_warm_sandbox")).lower() in ("true", "1", "yes")
        # Guards against double-copying sources within the same scan if
        # create_sandbox is called twice for one agent_id. In warm mode we do
        # NOT dedup across scans — the reset clears /workspace every time.
        self._warm_scan_ids: set[str] = set()
        # Tracks the active Caido project id so a new scan can switch away from
        # (and optionally delete) the previous one.
        self._current_caido_project_id: str | None = None

        if self._warm:
            import atexit

            atexit.register(self.shutdown)

    def _find_available_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("", 0))
            return cast("int", s.getsockname()[1])

    def _get_scan_id(self, agent_id: str) -> str:
        try:
            from strix.telemetry.tracer import get_global_tracer

            tracer = get_global_tracer()
            if tracer and tracer.scan_config:
                return str(tracer.scan_config.get("scan_id", "default-scan"))
        except (ImportError, AttributeError):
            pass
        return f"scan-{agent_id.split('-')[0]}"

    def _verify_image_available(self, image_name: str, max_retries: int = 3) -> None:
        for attempt in range(max_retries):
            try:
                image = self.client.images.get(image_name)
                if not image.id or not image.attrs:
                    raise ImageNotFound(f"Image {image_name} metadata incomplete")  # noqa: TRY301
            except (ImageNotFound, DockerException):
                if attempt == max_retries - 1:
                    raise
                time.sleep(2**attempt)
            else:
                return

    def _recover_container_state(self, container: Container) -> None:
        for env_var in container.attrs["Config"]["Env"]:
            if env_var.startswith("TOOL_SERVER_TOKEN="):
                self._tool_server_token = env_var.split("=", 1)[1]
                break

        port_bindings = container.attrs.get("NetworkSettings", {}).get("Ports", {})
        port_key = f"{CONTAINER_TOOL_SERVER_PORT}/tcp"
        if port_bindings.get(port_key):
            self._tool_server_port = int(port_bindings[port_key][0]["HostPort"])

        caido_port_key = f"{CONTAINER_CAIDO_PORT}/tcp"
        if port_bindings.get(caido_port_key):
            self._caido_port = int(port_bindings[caido_port_key][0]["HostPort"])

    def _wait_for_tool_server(self, max_retries: int = 30, timeout: int = 5) -> None:
        host = self._resolve_docker_host()
        health_url = f"http://{host}:{self._tool_server_port}/health"

        time.sleep(5)

        for attempt in range(max_retries):
            try:
                with httpx.Client(trust_env=False, timeout=timeout) as client:
                    response = client.get(health_url)
                    if response.status_code == 200:
                        data = response.json()
                        if data.get("status") == "healthy":
                            return
            except (httpx.ConnectError, httpx.TimeoutException, httpx.RequestError):
                pass

            time.sleep(min(2**attempt * 0.5, 5))

        raise SandboxInitializationError(
            "Tool server failed to start",
            "Container initialization timed out. Please try again.",
        )

    def _container_name(self, scan_id: str) -> str:
        """Stable name `strix-scan-warm` in warm mode (reused across scans),
        per-scan `strix-scan-{scan_id}` otherwise."""
        return "strix-scan-warm" if self._warm else f"strix-scan-{scan_id}"

    def _container_scan_label(self, scan_id: str) -> str:
        """Label value for `strix-scan-id` — constant in warm mode so a reused
        container keeps one label, scan-specific otherwise."""
        return "warm" if self._warm else scan_id

    def _create_container(self, scan_id: str, max_retries: int = 2) -> Container:
        container_name = self._container_name(scan_id)
        image_name = Config.get("strix_image")
        if not image_name:
            raise ValueError("STRIX_IMAGE must be configured")

        self._verify_image_available(image_name)

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                with contextlib.suppress(NotFound):
                    existing = self.client.containers.get(container_name)
                    with contextlib.suppress(Exception):
                        existing.stop(timeout=5)
                    existing.remove(force=True)
                    time.sleep(1)

                self._tool_server_port = self._find_available_port()
                self._caido_port = self._find_available_port()
                self._tool_server_token = secrets.token_urlsafe(32)
                execution_timeout = Config.get("strix_sandbox_execution_timeout") or "120"

                container = self.client.containers.run(
                    image_name,
                    command="sleep infinity",
                    detach=True,
                    name=container_name,
                    hostname=container_name,
                    ports={
                        f"{CONTAINER_TOOL_SERVER_PORT}/tcp": self._tool_server_port,
                        f"{CONTAINER_CAIDO_PORT}/tcp": self._caido_port,
                    },
                    cap_add=["NET_ADMIN", "NET_RAW"],
                    labels={"strix-scan-id": self._container_scan_label(scan_id)},
                    environment={
                        "PYTHONUNBUFFERED": "1",
                        "TOOL_SERVER_PORT": str(CONTAINER_TOOL_SERVER_PORT),
                        "TOOL_SERVER_TOKEN": self._tool_server_token,
                        "STRIX_SANDBOX_EXECUTION_TIMEOUT": str(execution_timeout),
                        "HOST_GATEWAY": HOST_GATEWAY_HOSTNAME,
                    },
                    extra_hosts={HOST_GATEWAY_HOSTNAME: "host-gateway"},
                    tty=True,
                )

                self._scan_container = container
                self._warm_scan_ids.clear()
                self._current_caido_project_id = None
                self._wait_for_tool_server()

            except (DockerException, RequestsConnectionError, RequestsTimeout) as e:
                last_error = e
                if attempt < max_retries:
                    self._tool_server_port = None
                    self._tool_server_token = None
                    self._caido_port = None
                    time.sleep(2**attempt)
            else:
                return container

        raise SandboxInitializationError(
            "Failed to create container",
            f"Container creation failed after {max_retries + 1} attempts: {last_error}",
        ) from last_error

    def _get_or_create_container(self, scan_id: str) -> Container:
        container_name = self._container_name(scan_id)

        if self._scan_container:
            try:
                self._scan_container.reload()
                if self._scan_container.status == "running":
                    return self._scan_container
            except NotFound:
                self._scan_container = None
                self._tool_server_port = None
                self._tool_server_token = None
                self._caido_port = None

        try:
            container = self.client.containers.get(container_name)
            container.reload()

            if container.status != "running":
                container.start()
                time.sleep(2)

            self._scan_container = container
            self._recover_container_state(container)
        except NotFound:
            pass
        else:
            return container

        try:
            containers = self.client.containers.list(
                all=True, filters={"label": f"strix-scan-id={scan_id}"}
            )
            if containers:
                container = containers[0]
                if container.status != "running":
                    container.start()
                    time.sleep(2)

                self._scan_container = container
                self._recover_container_state(container)
                return container
        except DockerException:
            pass

        return self._create_container(scan_id)

    def _copy_local_directory_to_container(
        self, container: Container, local_path: str, target_name: str | None = None
    ) -> None:
        import tarfile
        from io import BytesIO

        try:
            local_path_obj = Path(local_path).resolve()
            if not local_path_obj.exists() or not local_path_obj.is_dir():
                return

            tar_buffer = BytesIO()
            with tarfile.open(fileobj=tar_buffer, mode="w") as tar:
                for item in local_path_obj.rglob("*"):
                    if item.is_file():
                        rel_path = item.relative_to(local_path_obj)
                        arcname = Path(target_name) / rel_path if target_name else rel_path
                        tar.add(item, arcname=arcname)

            tar_buffer.seek(0)
            container.put_archive("/workspace", tar_buffer.getvalue())
            container.exec_run(
                "chown -R pentester:pentester /workspace && chmod -R 755 /workspace",
                user="root",
            )
        except (OSError, DockerException):
            pass

    async def create_sandbox(
        self,
        agent_id: str,
        existing_token: str | None = None,
        local_sources: list[dict[str, str]] | None = None,
    ) -> SandboxInfo:
        scan_id = self._get_scan_id(agent_id)
        container = self._get_or_create_container(scan_id)

        # In warm mode the container is reused across scans. Clear the previous
        # scan's state (terminal sessions, /workspace, Caido project) so the
        # new scan starts clean — this is the isolation mechanism that lets a
        # single long-lived container serve multiple scans safely.
        if self._warm and scan_id not in self._warm_scan_ids:
            await self._warm_reset(agent_id, scan_id, container)
            self._warm_scan_ids.add(scan_id)

        # Guard against double-copying sources within the same scan if
        # create_sandbox is called twice for one agent_id (e.g. subagent reuse).
        # In non-warm mode this also dedups the very first copy for a scan_id.
        should_copy_sources = scan_id not in self._warm_scan_ids or not self._warm
        if local_sources and should_copy_sources:
            for index, source in enumerate(local_sources, start=1):
                source_path = source.get("source_path")
                if not source_path:
                    continue
                target_name = (
                    source.get("workspace_subdir") or Path(source_path).name or f"target_{index}"
                )
                self._copy_local_directory_to_container(container, source_path, target_name)
            from strix.utils.resource_paths import get_strix_resource_path
            skills_dir = get_strix_resource_path("skills")
            if skills_dir.exists():
                self._copy_local_directory_to_container(container, str(skills_dir), "skills")

            if not self._warm:
                # Legacy per-scan dedup marker (cold mode only).
                setattr(self, f"_source_copied_{scan_id}", True)

        if container.id is None:
            raise RuntimeError("Docker container ID is unexpectedly None")

        token = existing_token or self._tool_server_token
        if self._tool_server_port is None or self._caido_port is None or token is None:
            raise RuntimeError("Tool server not initialized")

        host = self._resolve_docker_host()
        api_url = f"http://{host}:{self._tool_server_port}"

        await self._register_agent(api_url, agent_id, token)

        return {
            "workspace_id": container.id,
            "api_url": api_url,
            "auth_token": token,
            "tool_server_port": self._tool_server_port,
            "caido_port": self._caido_port,
            "agent_id": agent_id,
        }

    async def _register_agent(self, api_url: str, agent_id: str, token: str) -> None:
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    f"{api_url}/register_agent",
                    params={"agent_id": agent_id},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
                response.raise_for_status()
        except httpx.RequestError:
            pass

    async def get_sandbox_url(self, container_id: str, port: int) -> str:
        try:
            self.client.containers.get(container_id)
            return f"http://{self._resolve_docker_host()}:{port}"
        except NotFound:
            raise ValueError(f"Container {container_id} not found.") from None

    def _resolve_docker_host(self) -> str:
        docker_host = os.getenv("DOCKER_HOST", "")
        if docker_host:
            from urllib.parse import urlparse

            parsed = urlparse(docker_host)
            # Handle tcp://, http://, https:// AND ssh://
            if parsed.scheme in ("tcp", "http", "https", "ssh") and parsed.hostname:
                return parsed.hostname
        return "127.0.0.1"

    async def destroy_sandbox(self, container_id: str) -> None:
        # Note: never called by the agent (dead code in the cold path). Kept for
        # interface completeness; warm mode tears down via shutdown().
        try:
            container = self.client.containers.get(container_id)
            container.stop()
            container.remove()
            self._scan_container = None
            self._tool_server_port = None
            self._tool_server_token = None
            self._caido_port = None
        except (NotFound, DockerException):
            pass

    def cleanup(self) -> None:
        # In warm mode the container survives across CLI invocations — the
        # per-exit teardown at cli.py/tui.py must NOT kill it. Only clear the
        # in-process references; the actual container stays running.
        if self._warm:
            self._scan_container = None
            self._tool_server_port = None
            self._tool_server_token = None
            self._caido_port = None
            return

        if self._scan_container is not None:
            container_name = self._scan_container.name
            self._scan_container = None
            self._tool_server_port = None
            self._tool_server_token = None
            self._caido_port = None

            if container_name is None:
                return

            import subprocess

            subprocess.Popen(  # noqa: S603
                ["docker", "rm", "-f", container_name],  # noqa: S607
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

    def shutdown(self) -> None:
        """Explicit teardown of a warm container on process exit."""
        if self._scan_container is None:
            return
        container_name = self._scan_container.name
        self._scan_container = None
        self._tool_server_port = None
        self._tool_server_token = None
        self._caido_port = None
        self._warm_scan_ids.clear()
        self._current_caido_project_id = None

        if container_name is None:
            return

        import subprocess

        with contextlib.suppress(Exception):
            subprocess.run(  # noqa: S603
                ["docker", "rm", "-f", container_name],  # noqa: S607
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )

    async def _warm_reset(
        self, agent_id: str, scan_id: str, container: Container
    ) -> None:
        """Clear the previous scan's state from the warm container so the new
        scan starts isolated without restarting the container.

        Steps:
          1. Cancel the previous agent's in-flight tool tasks + terminal sessions
             via POST /reset_agent on the tool server.
          2. Wipe /workspace (the previous scan's copied sources).
          3. Create a fresh temporary Caido project and select it, so proxy
             captures from the new scan land in a clean project.
        """
        if self._tool_server_port is None or self._tool_server_token is None:
            raise RuntimeError("Tool server not initialized for warm reset")

        host = self._resolve_docker_host()
        api_url = f"http://{host}:{self._tool_server_port}"
        token = self._tool_server_token

        # Step 1 — cancel prev agent's terminal sessions + in-flight tool tasks.
        await self._call_reset_agent(api_url, agent_id, token)

        # Step 2 — wipe /workspace of the previous scan's sources. Keep the
        # directory itself (it is WORKDIR); restore ownership/permissions so the
        # pentester user can write to it after the fresh copy.
        with contextlib.suppress(Exception):
            container.exec_run(
                "find /workspace -mindepth 1 -delete",
                user="root",
            )
            container.exec_run(
                "chown -R pentester:pentester /workspace && chmod -R 755 /workspace",
                user="root",
            )

        # Step 3 — fresh Caido project for proxy isolation. Best-effort: if
        # Caido is unreachable (e.g. proxy tools unused this scan), the reset
        # still succeeds for the workspace + terminal state.
        if self._caido_port is not None:
            with contextlib.suppress(Exception):
                await self._switch_caido_project(scan_id, token)

    async def _call_reset_agent(self, api_url: str, agent_id: str, token: str) -> None:
        try:
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    f"{api_url}/reset_agent",
                    params={"agent_id": agent_id},
                    headers={"Authorization": f"Bearer {token}"},
                    timeout=30,
                )
                response.raise_for_status()
        except httpx.RequestError:
            # Reset is best-effort; a fresh container simply has nothing to reset.
            pass

    async def _switch_caido_project(self, scan_id: str, caido_token: str) -> None:
        """Create a fresh temporary Caido project and select it as current.

        The guest token is fetched fresh via loginAsGuest to sidestep TTL
        expiry on long-lived warm containers. selectProject is the isolation
        mechanism — new proxy captures land in the new project.
        """
        if self._caido_port is None:
            return
        host = self._resolve_docker_host()
        graphql_url = f"http://{host}:{self._caido_port}/graphql"

        async with httpx.AsyncClient(trust_env=False) as client:
            # Fresh guest token (instance-scoped, not project-scoped).
            login_resp = await client.post(
                graphql_url,
                json={
                    "query": "mutation LoginAsGuest { loginAsGuest { token { accessToken } } }"
                },
                timeout=30,
            )
            login_resp.raise_for_status()
            token = login_resp.json()["data"]["loginAsGuest"]["token"]["accessToken"]

            headers = {"Authorization": f"Bearer {token}"}

            # Optionally delete the previous scan's project to prevent
            # accumulation of temporary projects.
            prev_id = self._current_caido_project_id
            if prev_id:
                with contextlib.suppress(Exception):
                    await client.post(
                        graphql_url,
                        headers=headers,
                        json={
                            "query": "mutation DeleteProject($id: ID!) { deleteProject(id: $id) { deleted } }",
                            "variables": {"id": prev_id},
                        },
                        timeout=30,
                    )

            # Create + select the new project.
            create_resp = await client.post(
                graphql_url,
                headers=headers,
                json={
                    "query": (
                        "mutation CreateProject($name: String!) {"
                        " createProject(input: {name: $name, temporary: true})"
                        " { project { id } } }"
                    ),
                    "variables": {"name": f"scan-{scan_id}"},
                },
                timeout=30,
            )
            create_resp.raise_for_status()
            new_id = create_resp.json()["data"]["createProject"]["project"]["id"]

            select_resp = await client.post(
                graphql_url,
                headers=headers,
                json={
                    "query": (
                        "mutation SelectProject($id: ID!) {"
                        " selectProject(id: $id) { currentProject { project { id } } } }"
                    ),
                    "variables": {"id": new_id},
                },
                timeout=30,
            )
            select_resp.raise_for_status()

        self._current_caido_project_id = new_id
