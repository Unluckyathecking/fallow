"""Eligibility telemetry: the pure classifier and its opt-in gateway wiring.

The verdict is measurement only — logged, never routed on. These tests pin the
documented signal→verdict mapping and prove the flag defaults off with no field,
no cost, and a byte-for-byte-unchanged served path.
"""

from gateway_helpers import (
    ADMIN_KEY,
    CHAT_MODEL,
    buffered_handler,
    make_endpoint,
)

from fallow_coordinator.gateway import GatewayConfig, LogStatus
from fallow_coordinator.gateway.eligibility import Eligibility, classify_eligibility

_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
_ONE = {CHAT_MODEL: (make_endpoint("h1", 8001),)}
_NONE: dict[str, tuple] = {CHAT_MODEL: ()}

_LONG = "x " * 1500  # ~3000 chars ≈ 750 tokens of prose
_HUGE = "x " * 5000  # ~10000 chars, well past the large-context threshold
_CODE_BLOCK = "```python\n" + ("print('hello')\n" * 120) + "```"  # sizeable code


def _chat(*contents: str, model: str = CHAT_MODEL) -> dict:
    return {"model": model, "messages": [{"role": "user", "content": c} for c in contents]}


# --- the pure classifier -------------------------------------------------------


def test_short_prompt_is_local_ok() -> None:
    assert classify_eligibility(_chat("classify this sentiment: great")) is Eligibility.LOCAL_OK


def test_short_embedding_input_is_local_ok() -> None:
    body = {"model": CHAT_MODEL, "input": "a short string to embed"}
    assert classify_eligibility(body) is Eligibility.LOCAL_OK


def test_long_multifile_code_prompt_escalates() -> None:
    body = _chat("refactor these modules and explain the trade-offs\n" + _CODE_BLOCK + _LONG)
    assert classify_eligibility(body) is Eligibility.ESCALATE


def test_huge_context_escalates_even_without_code() -> None:
    assert classify_eligibility(_chat(_HUGE)) is Eligibility.ESCALATE


def test_long_thread_escalates() -> None:
    body = _chat(*[f"turn {i}" for i in range(10)])
    assert classify_eligibility(body) is Eligibility.ESCALATE


def test_large_model_id_escalates() -> None:
    assert classify_eligibility(_chat("hi", model="llama-3-70b")) is Eligibility.ESCALATE


def test_midsize_prose_is_unknown() -> None:
    # Bigger than a one-liner, no code, well under the large-context line: we decline
    # to guess — this band is the honest denominator, not a verdict.
    assert classify_eligibility(_chat(_LONG)) is Eligibility.UNKNOWN


def test_small_code_snippet_is_unknown() -> None:
    # Code present but tiny: not the bread-and-butter local case, not clearly heavy.
    assert classify_eligibility(_chat("fix this: ```x=1```")) is Eligibility.UNKNOWN


def test_multimodal_parts_are_unknown_not_local_ok() -> None:
    # A list-of-parts content passes chat validation but carries no extractable string,
    # so we must not read "zero chars" as "small" and count it local.
    body = {
        "model": CHAT_MODEL,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
    }
    assert classify_eligibility(body) is Eligibility.UNKNOWN


def test_embedding_token_array_is_unknown_not_local_ok() -> None:
    # Pre-tokenised embedding input (a list of int arrays) skips chat validation and
    # yields no text — it belongs in the honest middle band, not local_ok.
    body = {"model": CHAT_MODEL, "input": [[1, 2, 3, 4]]}
    assert classify_eligibility(body) is Eligibility.UNKNOWN


def test_verdicts_are_the_documented_strings() -> None:
    assert Eligibility.LOCAL_OK == "local_ok"
    assert Eligibility.ESCALATE == "escalate"
    assert Eligibility.UNKNOWN == "unknown"


# --- opt-in wiring through the gateway -----------------------------------------


async def test_off_by_default_attaches_nothing(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"id":"cmpl-1"}'), endpoints=_ONE
    )
    response = await harness.client.post("/v1/chat/completions", json=_chat("hi"), headers=_AUTH)
    assert response.status_code == 200
    assert response.content == b'{"id":"cmpl-1"}'  # served path byte-for-byte unchanged
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SERVED
    assert entry.eligibility is None


async def test_enabled_attaches_verdict_on_served(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"id":"cmpl-1"}'),
        endpoints=_ONE,
        config=GatewayConfig(admission_timeout_s=0, eligibility_telemetry=True),
    )
    response = await harness.client.post(
        "/v1/chat/completions", json=_chat("classify this: great"), headers=_AUTH
    )
    assert response.status_code == 200
    assert response.content == b'{"id":"cmpl-1"}'
    assert harness.log.entries[0].eligibility == "local_ok"


async def test_enabled_attaches_verdict_on_shed(build_gateway) -> None:
    harness = await build_gateway(
        upstream_handler=buffered_handler(b"{}"),
        endpoints=_NONE,  # registered model, zero replicas -> shed
        config=GatewayConfig(admission_timeout_s=0, eligibility_telemetry=True),
    )
    response = await harness.client.post("/v1/chat/completions", json=_chat(_HUGE), headers=_AUTH)
    assert response.status_code == 503
    entry = harness.log.entries[0]
    assert entry.status is LogStatus.SHED
    assert entry.eligibility == "escalate"


async def _huge_context(_key, _collection, _query, _k) -> tuple[str, ...]:
    return (_HUGE,)


async def test_rag_augmented_prompt_drives_the_verdict(build_gateway) -> None:
    # A short question that pulls in a large retrieved context must be judged on the
    # prompt actually sent to the replica (context prepended), not the tiny original.
    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"id":"cmpl-1"}'),
        endpoints=_ONE,
        config=GatewayConfig(admission_timeout_s=0, eligibility_telemetry=True),
        retriever=_huge_context,
    )
    body = {**_chat("hi"), "rag": {"collection": "policies", "k": 1}}
    response = await harness.client.post("/v1/chat/completions", json=body, headers=_AUTH)
    assert response.status_code == 200
    entry = harness.log.entries[0]
    assert entry.rag_k == 1
    assert entry.eligibility == "escalate"


async def test_without_rag_the_original_body_still_drives_the_verdict(build_gateway) -> None:
    # Same permissive retriever wired in, but no rag field: the short body is judged
    # on its own and the retriever is never consulted.
    harness = await build_gateway(
        upstream_handler=buffered_handler(b'{"id":"cmpl-1"}'),
        endpoints=_ONE,
        config=GatewayConfig(admission_timeout_s=0, eligibility_telemetry=True),
        retriever=_huge_context,
    )
    response = await harness.client.post("/v1/chat/completions", json=_chat("hi"), headers=_AUTH)
    assert response.status_code == 200
    entry = harness.log.entries[0]
    assert entry.rag_k is None
    assert entry.eligibility == "local_ok"
