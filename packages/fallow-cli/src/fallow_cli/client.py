"""Typed HTTP client for the coordinator admin API (``/v1/admin/*``).

One method per route in ``docs/admin-api.md``. The :class:`httpx.Client` is
injected so tests drive it with ``httpx.MockTransport`` (no real network). Every
HTTP failure is translated into a :class:`CliError` with a user-friendly
message; the caller never sees an httpx exception or a traceback.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from types import TracebackType
from typing import Any

import httpx
from pydantic import ValidationError

from fallow_cli.errors import EXIT_AUTH, CliError
from fallow_cli.models import (
    ApiKeyRequest,
    ApiKeyResponse,
    AssignmentRequest,
    EnrollmentTokenInfo,
    EnrollmentTokenResponse,
    ModelRegisterRequest,
    SiteJoinBundle,
    SiteJoinBundlesResponse,
)
from fallow_protocol import AgentSnapshot, JobStatus, JobSubmit, ModelManifest

_ADMIN_PREFIX = "/v1/admin"


class AdminClient:
    """Thin, typed wrapper over the coordinator's admin API."""

    def __init__(self, client: httpx.Client, admin_key: str) -> None:
        self._client = client
        self._headers = {"Authorization": f"Bearer {admin_key}"}
        self._base_url = str(client.base_url)

    def __enter__(self) -> AdminClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    # ── Enrollment & keys ────────────────────────────────────────────────
    def create_enrollment_token(self) -> str:
        resp = self._send("POST", "/enrollment_tokens", expected=(200, 201))
        return EnrollmentTokenResponse.model_validate(_json(resp)).token

    def list_enrollment_tokens(self) -> tuple[EnrollmentTokenInfo, ...]:
        resp = self._send("GET", "/enrollment_tokens", expected=(200,))
        return tuple(EnrollmentTokenInfo.model_validate(item) for item in _json_list(resp))

    def revoke_enrollment_token(self, token_id: str) -> None:
        self._send("DELETE", f"/enrollment_tokens/{token_id}", expected=(204,))

    def revoke_agent(self, agent_id: str) -> None:
        self._send("POST", f"/agents/{agent_id}/revoke", expected=(204,))

    def create_api_key(
        self,
        name: str,
        model_allowlist: tuple[str, ...] | None,
        rpm_limit: int | None = None,
        daily_limit: int | None = None,
    ) -> str:
        body = ApiKeyRequest(
            name=name,
            model_allowlist=model_allowlist,
            rpm_limit=rpm_limit,
            daily_limit=daily_limit,
        )
        resp = self._send(
            "POST",
            "/api_keys",
            json=body.model_dump(mode="json", exclude_none=True),
            expected=(200, 201),
        )
        return ApiKeyResponse.model_validate(_json(resp)).key

    # ── Agents & models ──────────────────────────────────────────────────
    def list_agents(self) -> tuple[AgentSnapshot, ...]:
        resp = self._send("GET", "/agents", expected=(200,))
        return tuple(AgentSnapshot.model_validate(item) for item in _json_list(resp))

    def list_models(self) -> tuple[ModelManifest, ...]:
        resp = self._send("GET", "/models", expected=(200,))
        return tuple(ModelManifest.model_validate(item) for item in _json_list(resp))

    def register_model(self, manifest: ModelManifest, blob_path: str) -> None:
        body = ModelRegisterRequest(manifest=manifest, blob_path=blob_path)
        self._send("POST", "/models", json=body.model_dump(mode="json"), expected=(201,))

    def set_assignments(self, model_id: str, agent_ids: tuple[str, ...]) -> None:
        body = AssignmentRequest(model_id=model_id, agent_ids=agent_ids)
        self._send("PUT", "/assignments", json=body.model_dump(mode="json"), expected=(204,))

    # ── Jobs ─────────────────────────────────────────────────────────────
    def submit_job(self, job: JobSubmit) -> JobStatus:
        resp = self._send("POST", "/jobs", json=job.model_dump(mode="json"), expected=(200, 201))
        return JobStatus.model_validate(_json(resp))

    def get_job(self, job_id: str) -> JobStatus:
        resp = self._send("GET", f"/jobs/{job_id}", expected=(200,))
        return JobStatus.model_validate(_json(resp))

    def create_site_join_bundles(self, count: int) -> tuple[SiteJoinBundle, ...]:
        resp = self._send("POST", "/site/join-bundles", json={"count": count}, expected=(201,))
        try:
            bundles = SiteJoinBundlesResponse.model_validate(_json(resp)).bundles
        except (ValidationError, ValueError) as exc:
            raise CliError("coordinator returned malformed Site Mode join bundles") from exc
        if len(bundles) != count:
            raise CliError(f"coordinator returned {len(bundles)} join bundles, expected {count}")
        return bundles

    # ── Transport ────────────────────────────────────────────────────────
    def _send(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        expected: Iterable[int],
    ) -> httpx.Response:
        try:
            resp = self._client.request(
                method, f"{_ADMIN_PREFIX}{path}", json=json, headers=self._headers
            )
        except httpx.RequestError as exc:
            raise CliError(f"coordinator unreachable at {self._base_url}") from exc
        if resp.status_code in (401, 403):
            raise CliError("admin key rejected", exit_code=EXIT_AUTH)
        if resp.status_code not in tuple(expected):
            raise CliError(_http_error_message(resp))
        return resp


def _json(resp: httpx.Response) -> Mapping[str, Any]:
    payload = _decode(resp)
    if not isinstance(payload, Mapping):
        raise CliError(f"coordinator returned an unexpected body for {resp.request.url}")
    return payload


def _json_list(resp: httpx.Response) -> list[Any]:
    payload = _decode(resp)
    if not isinstance(payload, list):
        raise CliError(f"coordinator returned an unexpected body for {resp.request.url}")
    return payload


def _decode(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        raise CliError(f"coordinator returned invalid JSON ({resp.status_code})") from exc


def _http_error_message(resp: httpx.Response) -> str:
    detail = _detail(resp)
    if detail:
        return f"coordinator error {resp.status_code}: {detail}"
    return f"coordinator error {resp.status_code}"


def _detail(resp: httpx.Response) -> str | None:
    try:
        body = resp.json()
    except ValueError:
        return resp.text.strip() or None
    if isinstance(body, Mapping):
        value = body.get("detail") or body.get("error")
        if isinstance(value, str):
            return value
    return None
