"""Static LAN Site Mode acceptance harness.

This module boots the *real* vertical for the outbound-only school pilot with no
external network: a pinned-HTTPS coordinator on an exact loopback address, a join
bundle minted through the ``flw`` CLI code path, the built Go Site runtime enrolled
once against a persisted token-free profile, and a loopback-only fake llama the Go
supervisor spawns. Requests ride the outbound claim relay end to end.

Everything is deterministic and self-contained so it runs in CI. The Go binary is
required (``FALLOW_GO_AGENT_BIN``); a missing binary fails loudly rather than
skipping, because a skipped acceptance lane is a failed acceptance run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import uvicorn
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from fallow_coordinator.app import CoordinatorConfig, create_app
from fallow_protocol.models import ModelManifest

LOOPBACK = "127.0.0.1"
# The advertised origin uses a dialable name (loopback IP literals are rejected as
# a public Site origin); the actual listener still binds the exact loopback IP.
SITE_HOST = "localhost"
HERE = Path(__file__).resolve().parent
FAKE_LLAMA_SRC = HERE / "fakellama.go"
_GO_AGENT_BIN_ENV = "FALLOW_GO_AGENT_BIN"


# The exact-head binary is built at most once per process and cached here so a
# session-scoped fixture (and every scenario it feeds) reuses it.
_BUILT_BINARY: Path | None = None


def _repo_root() -> Path:
    # HERE is tests/integration/site_mode; the repo root is three dirs up.
    return HERE.parents[2]


def build_go_agent_binary() -> Path:
    """Build ``cmd/agentctl`` from this exact head into a temp dir, or fail loudly.

    CI does not prebuild the Go agent, and a skipped Site Mode acceptance lane is a
    failed acceptance run, so when ``FALLOW_GO_AGENT_BIN`` is unset the harness
    builds the binary itself. ``CGO_ENABLED=0`` keeps the idle detector on the
    honest unsupported stub (a deterministic always-idle topology for CI). A
    missing Go toolchain or a build failure raises with the build's own stderr;
    it never silently skips.
    """
    global _BUILT_BINARY
    if _BUILT_BINARY is not None and _BUILT_BINARY.is_file():
        return _BUILT_BINARY
    go = shutil.which("go")
    if go is None:
        raise RuntimeError(
            f"{_GO_AGENT_BIN_ENV} is unset and no 'go' toolchain is on PATH to build "
            "cmd/agentctl; a skipped Site Mode acceptance lane is a failed acceptance run"
        )
    go_dir = _repo_root() / "go-agent"
    if not (go_dir / "cmd" / "agentctl").is_dir():
        raise RuntimeError(f"cannot locate go-agent/cmd/agentctl under {go_dir}")
    out_dir = Path(tempfile.mkdtemp(prefix="fallow-agentctl-"))
    name = "agentctl.exe" if os.name == "nt" else "agentctl"
    out = out_dir / name
    proc = subprocess.run(
        [go, "build", "-o", str(out), "./cmd/agentctl"],
        cwd=str(go_dir),
        capture_output=True,
        text=True,
        env=dict(os.environ, CGO_ENABLED="0"),
    )
    if proc.returncode != 0 or not out.is_file():
        detail = (
            proc.stderr.strip() or proc.stdout.strip()
        ) or f"go build exited {proc.returncode}"
        raise RuntimeError(
            f"failed to build cmd/agentctl for the Site Mode acceptance lane:\n{detail}"
        )
    _BUILT_BINARY = out
    return out


def go_agent_binary() -> Path:
    """The Go agent binary: the prebuilt one if provided, else built from this head.

    A skipped Site Mode acceptance lane is a failed acceptance run, so this never
    skips: an explicit ``FALLOW_GO_AGENT_BIN`` must point at a real file, and when
    it is absent the binary is built from the exact head (failing loudly if Go or
    the build is unavailable).
    """
    raw = os.environ.get(_GO_AGENT_BIN_ENV)
    if raw:
        binary = Path(raw)
        if not binary.is_file():
            raise RuntimeError(f"{_GO_AGENT_BIN_ENV}={raw} is not a file")
        return binary
    return build_go_agent_binary()


def reserve_loopback_sockets() -> tuple[list[socket.socket], int]:
    """Bind both loopback families on one shared port for the static listener.

    The exact IPv4 loopback socket fixes the port; a matching IPv6 loopback socket
    is bound to the same port so a client resolving ``localhost`` to either family
    reaches the coordinator deterministically. Both are exact, non-wildcard
    loopback binds — nothing listens on a LAN interface.
    """
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    v4.bind((LOOPBACK, 0))
    port = int(v4.getsockname()[1])
    socks = [v4]
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        v6.bind(("::1", port))
        socks.append(v6)
    except OSError:
        pass  # IPv6 loopback unavailable; the IPv4 socket still serves localhost
    return socks, port


def write_tls_cert(directory: Path) -> tuple[Path, Path]:
    """Write a short-lived self-signed EC cert/key pinned by the coordinator.

    The leaf carries the advertised ``localhost`` name plus the loopback IPs as
    SANs so a strict CA-verifying admin client is satisfied; the Site Mode agent
    trusts it by SPKI pin, not by CA chain or name.
    """
    import ipaddress

    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, SITE_HOST)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName(SITE_HOST),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.IPAddress(ipaddress.ip_address("::1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile = directory / "site-cert.pem"
    keyfile = directory / "site-key.pem"
    certfile.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


def _ip(host: str) -> object:
    import ipaddress

    return ipaddress.ip_address(host)


def make_site_config(
    tmp: Path, port: int, certfile: Path, keyfile: Path, **overrides: object
) -> CoordinatorConfig:
    origin = f"https://{SITE_HOST}:{port}"
    base: dict[str, object] = {
        "db_path": tmp / "coordinator.db",
        "blob_dir": tmp / "blobs",
        "unit_input_dir": tmp / "units",
        "result_dir": tmp / "results",
        "events_jsonl_path": tmp / "events.jsonl",
        "gateway_log_path": tmp / "gateway.jsonl",
        "admin_key": "site-admin-key",
        "host": LOOPBACK,
        "port": port,
        "requeue_interval_s": 3600.0,
        "poll_sleep_s": 0.01,
        "admission_timeout_s": 0,
        "site": {
            "enabled": True,
            "site_id": "school-pilot",
            "public_urls": (origin,),
            "tls_certfile": certfile,
            "tls_keyfile": keyfile,
        },
    }
    base.update(overrides)
    return CoordinatorConfig.model_validate(base)


@dataclass
class SiteCoordinator:
    """One pinned-HTTPS coordinator served over an exact loopback socket."""

    base_url: str
    port: int
    config: CoordinatorConfig
    certfile: Path
    keyfile: Path
    client: httpx.AsyncClient  # a pin/CA-trusting admin+gateway client
    app: object

    def admin_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.config.admin_key}"}


def bind_fixed_loopback(port: int) -> list[socket.socket]:
    """Rebind both loopback families on an exact known port (for a restart)."""
    socks: list[socket.socket] = []
    v4 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    v4.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    v4.bind((LOOPBACK, port))
    socks.append(v4)
    try:
        v6 = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        v6.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        v6.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        v6.bind(("::1", port))
        socks.append(v6)
    except OSError:
        pass
    return socks


@contextlib.asynccontextmanager
async def serve_site_coordinator(
    tmp: Path,
    *,
    port: int | None = None,
    certfile: Path | None = None,
    keyfile: Path | None = None,
    **overrides: object,
) -> AsyncIterator[SiteCoordinator]:
    """Serve a real site-enabled coordinator over TLS on an exact loopback port.

    Passing ``port``/``certfile``/``keyfile`` (and the same ``tmp`` for ``db_path``)
    restarts the *same* coordinator origin so a running agent reconnects to it.
    """
    if certfile is None or keyfile is None:
        certfile, keyfile = write_tls_cert(tmp)
    if port is None:
        socks, port = reserve_loopback_sockets()
    else:
        socks = bind_fixed_loopback(port)
    config = make_site_config(tmp, port, certfile, keyfile, **overrides)
    app = create_app(config)

    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="warning",
            lifespan="on",
            ssl_certfile=str(certfile),
            ssl_keyfile=str(keyfile),
        )
    )
    serve_task = asyncio.create_task(server.serve(sockets=socks))
    base_url = f"https://{SITE_HOST}:{port}"
    # A client that trusts the coordinator's leaf by CA file (verification against
    # the exact self-signed cert), used for admin setup and client-facing requests.
    verify = ssl.create_default_context(cafile=str(certfile))
    verify.check_hostname = True
    client = httpx.AsyncClient(base_url=base_url, verify=verify, trust_env=False, timeout=30.0)
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield SiteCoordinator(base_url, port, config, certfile, keyfile, client, app)
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task


def mint_join_bundle_via_flw(coord: SiteCoordinator, output_dir: Path) -> Path:
    """Mint one join file through the real ``flw site join-bundles`` code path.

    ``flw`` is exercised in-process via typer's runner with a TLS transport that
    trusts the coordinator's pinned leaf — the same admin-transport seam the CLI's
    own tests use — so the whole CLI join path runs: token mint, atomic no-clobber
    write, owner-only permissions. The subprocess binary is not used only because
    it cannot be handed a trust anchor for a sandbox self-signed cert; every line
    of join-bundle logic is the production CLI's.
    """
    from typer.testing import CliRunner

    import fallow_cli.main as flw_main

    verify = ssl.create_default_context(cafile=str(coord.certfile))
    transport = httpx.HTTPTransport(verify=verify)
    output_dir.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    prev = flw_main._ADMIN_TRANSPORT
    flw_main._ADMIN_TRANSPORT = transport
    try:
        result = runner.invoke(
            flw_main.app,
            [
                "--coordinator-url",
                coord.base_url,
                "site",
                "join-bundles",
                "--count",
                "1",
                "--output",
                str(output_dir),
            ],
            env={"FLW_ADMIN_KEY": coord.config.admin_key},
        )
    finally:
        flw_main._ADMIN_TRANSPORT = prev
    if result.exit_code != 0:
        raise RuntimeError(f"flw site join-bundles failed ({result.exit_code}): {result.output}")
    bundle = output_dir / "desk-01.fallow-join"
    if not bundle.is_file():
        raise RuntimeError(f"flw did not write {bundle}: {result.output}")
    return bundle


def write_agent_toml(
    path: Path,
    *,
    join_bundle: Path,
    state_path: Path,
    cache_dir: Path,
    llama_binary: str,
    bind_host: str = LOOPBACK,
    port_start: int = 8100,
) -> None:
    """Write the Site Mode agent TOML the Go daemon reads.

    ``coordinator_url`` is deliberately absent: Site Mode dials the pinned origin
    from the join profile, and the bind host is loopback so replicas never leave
    the machine.
    """
    path.write_text(
        "\n".join(
            (
                f'site_join_bundle = "{join_bundle.as_posix()}"',
                f'bind_host = "{bind_host}"',
                f'llama_server_binary = "{Path(llama_binary).as_posix()}"',
                f'state_path = "{state_path.as_posix()}"',
                f'cache_dir = "{cache_dir.as_posix()}"',
                "work_poll_timeout_s = 2.0",
                "active_sleep_s = 0.2",
                "[port_range]",
                f"start = {port_start}",
                "count = 8",
                "",
            )
        ),
        encoding="utf-8",
    )


_FAKE_LLAMA_BIN: Path | None = None


def build_fake_llama() -> Path:
    """Build the native fake llama once with the same Go toolchain as agentctl.

    The supervisor spawns ``llama_server_binary`` as ``argv[0]``, so the fake must
    be a directly-executable native program on every platform (a ``.py`` cannot be
    spawned as ``argv[0]`` on Windows). It is a single stdlib-only Go file, so the
    build needs no module context. A missing toolchain or a failed build raises
    with the build's stderr rather than skipping.
    """
    global _FAKE_LLAMA_BIN
    if _FAKE_LLAMA_BIN is not None and _FAKE_LLAMA_BIN.is_file():
        return _FAKE_LLAMA_BIN
    go = shutil.which("go")
    if go is None:
        raise RuntimeError(
            f"{_GO_AGENT_BIN_ENV} is unset and no 'go' toolchain is on PATH to build the "
            "fake llama; a skipped Site Mode acceptance lane is a failed acceptance run"
        )
    out_dir = Path(tempfile.mkdtemp(prefix="fallow-fakellama-"))
    name = "fake-llama.exe" if os.name == "nt" else "fake-llama"
    out = out_dir / name
    proc = subprocess.run(
        [go, "build", "-o", str(out), str(FAKE_LLAMA_SRC)],
        capture_output=True,
        text=True,
        env=dict(os.environ, CGO_ENABLED="0"),
    )
    if proc.returncode != 0 or not out.is_file():
        detail = (
            proc.stderr.strip() or proc.stdout.strip()
        ) or f"go build exited {proc.returncode}"
        raise RuntimeError(f"failed to build the fake llama for the Site Mode lane:\n{detail}")
    _FAKE_LLAMA_BIN = out
    return out


def llama_command() -> str:
    """The path to the built native fake llama, used as ``llama_server_binary``."""
    return str(build_fake_llama())


@dataclass
class SiteDaemon:
    """A running ``agentctl run`` Site Mode daemon under test."""

    proc: asyncio.subprocess.Process
    state_path: Path
    _stderr: bytes = b""
    _rc: int | None = None

    async def stop(self) -> int:
        if self._rc is not None:
            return self._rc
        if self.proc.returncode is None:
            if sys.platform == "win32":
                self.proc.terminate()
            else:
                import signal

                self.proc.send_signal(signal.SIGINT)
        try:
            _, self._stderr = await asyncio.wait_for(self.proc.communicate(), timeout=15.0)
        except TimeoutError:
            self.proc.kill()
            _, self._stderr = await self.proc.communicate()
        self._rc = self.proc.returncode
        return self._rc or 0

    @property
    def stderr(self) -> str:
        return self._stderr.decode(errors="replace")

    def identity(self) -> dict:
        """The persisted token-free site identity, or {} before enrollment."""
        if not self.state_path.is_file():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))


@contextlib.asynccontextmanager
async def run_site_daemon(
    binary: Path, config_path: Path, state_path: Path, *, env: dict | None = None
) -> AsyncIterator[SiteDaemon]:
    """Launch ``agentctl run`` in Site Mode and guarantee a clean stop.

    ``env`` overlays the process environment — used to prove the pinned client
    ignores proxy variables by poisoning them and still enrolling.
    """
    proc_env = dict(os.environ)
    if env:
        proc_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        str(binary),
        "run",
        "-config",
        str(config_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    daemon = SiteDaemon(proc, state_path)
    try:
        yield daemon
    finally:
        with contextlib.suppress(Exception):
            await daemon.stop()


async def wait_for(predicate, *, timeout: float, interval: float = 0.05, what: str = "condition"):
    """Poll ``predicate`` (sync or async) until truthy, or fail with a diagnostic."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        last = predicate()
        if asyncio.iscoroutine(last):
            last = await last
        if last:
            return last
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}; last={last!r}")


# ── coordinator admin setup over the trusting client ─────────────────────────

CHAT_MODEL = "qwen2.5-7b"


def make_chat_manifest(blob: Path) -> ModelManifest:
    """A CHAT manifest whose sha256/size match ``blob`` so the agent verifies it."""
    data = blob.read_bytes()
    return ModelManifest(
        model_id=CHAT_MODEL,
        family="qwen2.5",
        quant="Q4_K_M",
        worker_kind="chat",
        file_name=f"{CHAT_MODEL}.gguf",
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        min_ram_mb=0,
        min_vram_mb=0,
    )


async def register_chat_model(coord: SiteCoordinator, blob: Path) -> None:
    manifest = make_chat_manifest(blob)
    resp = await coord.client.post(
        "/v1/admin/models",
        json={"manifest": manifest.model_dump(mode="json"), "blob_path": str(blob)},
        headers=coord.admin_headers(),
    )
    if resp.status_code != 201:
        raise RuntimeError(f"register model failed {resp.status_code}: {resp.text}")


async def assign_model(
    coord: SiteCoordinator, agent_ids: list[str], model_id: str = CHAT_MODEL
) -> None:
    resp = await coord.client.put(
        "/v1/admin/assignments",
        json={"model_id": model_id, "agent_ids": agent_ids},
        headers=coord.admin_headers(),
    )
    if resp.status_code != 204:
        raise RuntimeError(f"assign failed {resp.status_code}: {resp.text}")


async def create_api_key(coord: SiteCoordinator, name: str = "pilot") -> str:
    resp = await coord.client.post(
        "/v1/admin/api_keys", json={"name": name}, headers=coord.admin_headers()
    )
    if resp.status_code != 201:
        raise RuntimeError(f"api key failed {resp.status_code}: {resp.text}")
    return str(resp.json()["key"])


async def list_agents(coord: SiteCoordinator) -> list[dict]:
    resp = await coord.client.get("/v1/admin/agents", headers=coord.admin_headers())
    if resp.status_code != 200:
        raise RuntimeError(f"list agents failed {resp.status_code}: {resp.text}")
    return list(resp.json())


async def wait_enrolled(coord: SiteCoordinator, *, timeout: float = 20.0) -> str:
    """Wait until exactly one agent has enrolled and return its id."""

    async def _one() -> str | None:
        agents = await list_agents(coord)
        return str(agents[0]["agent_id"]) if agents else None

    return await wait_for(_one, timeout=timeout, what="agent enrollment")


def _ready_chat_replica(agent: dict, model_id: str = CHAT_MODEL) -> dict | None:
    for replica in agent.get("replicas", ()):
        if replica.get("model_id") == model_id and replica.get("state") == "ready":
            return replica
    return None


async def wait_replica_ready(
    coord: SiteCoordinator, agent_id: str, *, timeout: float = 40.0, model_id: str = CHAT_MODEL
) -> int:
    """Wait until the agent advertises a READY loopback replica; return its port."""

    async def _ready() -> int | None:
        for agent in await list_agents(coord):
            if agent["agent_id"] != agent_id:
                continue
            replica = _ready_chat_replica(agent, model_id)
            return int(replica["port"]) if replica else None
        return None

    return await wait_for(_ready, timeout=timeout, what="READY replica")


# ── one-shot agentctl controls (reclaim / release / doctor) ──────────────────


async def agentctl(binary: Path, *args: str, env: dict | None = None) -> tuple[int, str, str]:
    """Run one one-shot ``agentctl`` subcommand; return (rc, stdout, stderr)."""
    proc_env = dict(os.environ)
    if env:
        proc_env.update(env)
    proc = await asyncio.create_subprocess_exec(
        str(binary),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=proc_env,
    )
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def reclaim(binary: Path, config: Path) -> None:
    rc, _out, err = await agentctl(binary, "reclaim", "-config", str(config))
    if rc != 0:
        raise RuntimeError(f"agentctl reclaim failed ({rc}): {err}")


async def release(binary: Path, config: Path) -> None:
    rc, _out, err = await agentctl(binary, "release", "-config", str(config))
    if rc != 0:
        raise RuntimeError(f"agentctl release failed ({rc}): {err}")


async def chat_once(coord: SiteCoordinator, key: str, *, echo: str = "world", stream: bool = False):
    """Send one client-facing OpenAI request through the gateway/relay."""
    return await coord.client.post(
        "/v1/chat/completions",
        json={
            "model": CHAT_MODEL,
            "stream": stream,
            "_echo": echo,
            "messages": [{"role": "user", "content": "hi"}],
        },
        headers={"Authorization": f"Bearer {key}"},
    )


async def agent_snapshot(coord: SiteCoordinator, agent_id: str) -> dict | None:
    for agent in await list_agents(coord):
        if agent["agent_id"] == agent_id:
            return agent
    return None


async def wait_serving_paused(
    coord: SiteCoordinator, agent_id: str, want: bool, *, timeout: float = 15.0
) -> None:
    async def _cond() -> bool:
        snap = await agent_snapshot(coord, agent_id)
        return bool(snap and snap.get("serving_paused") == want)

    await wait_for(_cond, timeout=timeout, what=f"serving_paused={want}")


# ── trust-boundary helpers ───────────────────────────────────────────────────


async def doctor(binary: Path, config: Path) -> dict:
    """Run ``agentctl doctor`` and return its parsed JSON report."""
    rc, out, err = await agentctl(binary, "doctor", "-config", str(config))
    text = out.strip() or err.strip()
    try:
        return {"_rc": rc, **json.loads(text)}
    except json.JSONDecodeError:
        return {"_rc": rc, "_raw": text}


# A syntactically valid SPKI pin that does not match the coordinator's leaf: the
# base64 of 32 zero bytes. Used to prove a wrong pin fails closed.
WRONG_PIN = "sha256/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


def corrupt_join_pin(join: Path, pin: str = WRONG_PIN) -> None:
    """Rewrite a minted join file's pin so the agent's pin check must fail."""
    bundle = json.loads(join.read_text(encoding="utf-8"))
    bundle["coordinator_spki_sha256"] = [pin]
    join.write_text(json.dumps(bundle, separators=(",", ":")) + "\n", encoding="utf-8")


async def wait_process_exit(daemon: SiteDaemon, *, timeout: float = 15.0) -> int:
    """Wait for a daemon that is expected to exit on its own (e.g. bad enrollment)."""
    try:
        await asyncio.wait_for(daemon.proc.wait(), timeout=timeout)
    except TimeoutError as exc:
        raise AssertionError("daemon did not exit as expected") from exc
    _out, err = await daemon.proc.communicate()
    daemon._stderr = err
    daemon._rc = daemon.proc.returncode
    return daemon._rc or 0


# ── legacy direct-mode parity (Site Mode is additive and off by default) ─────


def make_plain_config(tmp: Path, port: int, **overrides: object) -> CoordinatorConfig:
    """A non-site coordinator config: plain HTTP, no relay, no join minting."""
    base: dict[str, object] = {
        "db_path": tmp / "coordinator.db",
        "blob_dir": tmp / "blobs",
        "unit_input_dir": tmp / "units",
        "result_dir": tmp / "results",
        "events_jsonl_path": tmp / "events.jsonl",
        "gateway_log_path": tmp / "gateway.jsonl",
        "admin_key": "site-admin-key",
        "host": LOOPBACK,
        "port": port,
        "requeue_interval_s": 3600.0,
        "poll_sleep_s": 0.01,
        "admission_timeout_s": 0,
    }
    base.update(overrides)
    return CoordinatorConfig.model_validate(base)


@contextlib.asynccontextmanager
async def serve_plain_coordinator(tmp: Path, **overrides: object) -> AsyncIterator[SiteCoordinator]:
    """Serve a non-site coordinator over plain HTTP loopback (legacy direct path)."""
    socks, port = reserve_loopback_sockets()
    config = make_plain_config(tmp, port, **overrides)
    app = create_app(config)
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    serve_task = asyncio.create_task(server.serve(sockets=socks))
    base_url = f"http://{LOOPBACK}:{port}"
    client = httpx.AsyncClient(base_url=base_url, trust_env=False, timeout=30.0)
    try:
        while not server.started:
            await asyncio.sleep(0.01)
        yield SiteCoordinator(base_url, port, config, tmp / "none", tmp / "none", client, app)
    finally:
        await client.aclose()
        server.should_exit = True
        with contextlib.suppress(Exception):
            await serve_task


async def mint_direct_token(coord: SiteCoordinator) -> str:
    resp = await coord.client.post("/v1/admin/enrollment_tokens", headers=coord.admin_headers())
    if resp.status_code != 201:
        raise RuntimeError(f"mint token failed {resp.status_code}: {resp.text}")
    return str(resp.json()["token"])


def write_direct_agent_toml(
    path: Path,
    *,
    coordinator_url: str,
    enrollment_token: str,
    state_path: Path,
    cache_dir: Path,
    llama_binary: str,
    bind_host: str = LOOPBACK,
    port_start: int = 8200,
) -> None:
    """Write a legacy direct-mode agent TOML (explicit coordinator_url, no join)."""
    path.write_text(
        "\n".join(
            (
                f'coordinator_url = "{coordinator_url}"',
                f'enrollment_token = "{enrollment_token}"',
                f'bind_host = "{bind_host}"',
                f'llama_server_binary = "{Path(llama_binary).as_posix()}"',
                f'state_path = "{state_path.as_posix()}"',
                f'cache_dir = "{cache_dir.as_posix()}"',
                "work_poll_timeout_s = 2.0",
                "active_sleep_s = 0.2",
                "[port_range]",
                f"start = {port_start}",
                "count = 8",
                "",
            )
        ),
        encoding="utf-8",
    )
