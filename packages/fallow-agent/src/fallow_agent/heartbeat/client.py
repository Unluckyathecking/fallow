"""``CoordinatorClient``: the agent's typed HTTP client to the coordinator.

Every agent→coordinator call goes through here. The client is a *stateful
connection object* — it holds the ``agent_id`` and bearer ``device_token``
learned at registration (connection state, not domain data; the wire messages
themselves stay frozen). All I/O is through an injected ``httpx.AsyncClient`` so
tests drive it with an ``httpx.MockTransport`` and never open a socket.

Retry policy (see ADR 009): idempotent calls (``heartbeat``, ``poll_work``)
retry *transport* failures with injected sleep + exponential backoff.
``register`` is never retried (a duplicate enrollment is not idempotent). 5xx
responses map to :class:`CoordinatorTransientError` but are not retried in-line;
the caller (heartbeat loop / event sink) decides how to react.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NoReturn, TypeVar

import httpx
from pydantic import ValidationError

from fallow_agent.heartbeat.config import ClientRetryConfig
from fallow_agent.heartbeat.constants import (
    ACCEPT_CODES,
    AUTH_CODES,
    EVENTS_PATH_TEMPLATE,
    HEARTBEAT_PATH_TEMPLATE,
    HTTP_NO_CONTENT,
    OK_CODES,
    REGISTER_PATH,
    RESULT_PATH_TEMPLATE,
    SERVER_ERROR_MIN,
    WORK_PATH_TEMPLATE,
    WORK_TIMEOUT_PARAM,
)
from fallow_agent.heartbeat.errors import (
    CoordinatorAuthError,
    CoordinatorProtocolError,
    CoordinatorTransientError,
)
from fallow_protocol.base import FallowModel
from fallow_protocol.messages import (
    AgentEvent,
    Heartbeat,
    HeartbeatResponse,
    RegisterRequest,
    RegisterResponse,
    WorkResult,
    WorkUnitLease,
)

SleepFn = Callable[[float], Awaitable[None]]
_T = TypeVar("_T", bound=FallowModel)


class CoordinatorClient:
    """Typed, retrying HTTP client for the coordinator's agent API."""

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient,
        agent_id: str | None = None,
        device_token: str | None = None,
        retry: ClientRetryConfig | None = None,
        sleep: SleepFn = asyncio.sleep,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._client = client
        self._agent_id = agent_id
        self._device_token = device_token
        self._retry = retry or ClientRetryConfig()
        self._sleep = sleep

    @property
    def agent_id(self) -> str | None:
        return self._agent_id

    @property
    def device_token(self) -> str | None:
        return self._device_token

    # ── Registration (never retried, no bearer) ──────────────────────────────

    async def register(self, request: RegisterRequest) -> RegisterResponse:
        """Enroll this machine. On success, stores agent_id + device_token."""
        url = self._base + REGISTER_PATH
        try:
            resp = await self._client.post(url, json=request.model_dump(mode="json"))
        except httpx.TransportError as exc:
            raise CoordinatorTransientError(f"register transport error: {exc}") from exc
        response = self._parse_ok(resp, RegisterResponse)
        self._agent_id = response.agent_id
        self._device_token = response.device_token
        return response

    # ── Heartbeat / work (idempotent, retried) ───────────────────────────────

    async def heartbeat(self, heartbeat: Heartbeat) -> HeartbeatResponse:
        url = self._base + HEARTBEAT_PATH_TEMPLATE.format(agent_id=heartbeat.agent_id)
        resp = await self._send_idempotent("POST", url, json_body=heartbeat.model_dump(mode="json"))
        return self._parse_ok(resp, HeartbeatResponse)

    async def poll_work(self, timeout_s: float) -> WorkUnitLease | None:
        url = self._base + WORK_PATH_TEMPLATE.format(agent_id=self._require_agent_id())
        resp = await self._send_idempotent("GET", url, params={WORK_TIMEOUT_PARAM: timeout_s})
        if resp.status_code == HTTP_NO_CONTENT:
            return None
        return self._parse_ok(resp, WorkUnitLease)

    # ── Fire-and-forget writes (not retried in-line) ─────────────────────────

    async def push_event(self, event: AgentEvent) -> None:
        url = self._base + EVENTS_PATH_TEMPLATE.format(agent_id=event.agent_id)
        await self._post_expect_accept(url, event)

    async def complete_unit(self, result: WorkResult) -> None:
        url = self._base + RESULT_PATH_TEMPLATE.format(
            agent_id=self._require_agent_id(), unit_id=result.work_unit_id
        )
        await self._post_expect_accept(url, result)

    # ── internals ────────────────────────────────────────────────────────────

    def _require_agent_id(self) -> str:
        if self._agent_id is None:
            raise CoordinatorProtocolError("agent_id unknown; call register() first")
        return self._agent_id

    def _auth_headers(self) -> dict[str, str]:
        if self._device_token is None:
            raise CoordinatorProtocolError("device token unset; call register() first")
        return {"Authorization": f"Bearer {self._device_token}"}

    async def _send_idempotent(
        self,
        method: str,
        url: str,
        *,
        json_body: Any = None,
        params: Mapping[str, float] | None = None,
    ) -> httpx.Response:
        attempt = 0
        while True:
            headers = self._auth_headers()
            try:
                return await self._client.request(
                    method, url, headers=headers, json=json_body, params=params
                )
            except httpx.TransportError as exc:
                attempt += 1
                if attempt > self._retry.max_retries:
                    raise CoordinatorTransientError(
                        f"{method} {url} failed after {attempt} attempt(s): {exc}"
                    ) from exc
                await self._sleep(self._retry.backoff_base_s * (2 ** (attempt - 1)))

    async def _post_expect_accept(self, url: str, body: FallowModel) -> None:
        try:
            resp = await self._client.post(
                url, headers=self._auth_headers(), json=body.model_dump(mode="json")
            )
        except httpx.TransportError as exc:
            raise CoordinatorTransientError(f"POST {url} transport error: {exc}") from exc
        if resp.status_code in ACCEPT_CODES:
            return
        self._classify_failure(resp.status_code)

    def _parse_ok(self, resp: httpx.Response, model: type[_T]) -> _T:
        if resp.status_code in OK_CODES:
            try:
                return model.model_validate_json(resp.content)
            except ValidationError as exc:
                raise CoordinatorProtocolError(f"malformed {model.__name__} body: {exc}") from exc
        self._classify_failure(resp.status_code)

    @staticmethod
    def _classify_failure(code: int) -> NoReturn:
        if code in AUTH_CODES:
            raise CoordinatorAuthError(f"coordinator rejected credentials ({code})")
        if code >= SERVER_ERROR_MIN:
            raise CoordinatorTransientError(f"coordinator server error {code}")
        raise CoordinatorProtocolError(f"unexpected coordinator status {code}")
