# RAG queries

Fallow embeds a query on a healthy fleet replica, then searches the named
sqlite-vec collection. The collection decides which embedding model is used.

## HTTP contract

Send a Fallow API key as a bearer token:

```http
POST /v1/rag/collections/policies/query
Authorization: Bearer flw_...
Content-Type: application/json

{"q":"What is the travel policy?","k":4}
```

`q` must contain non-whitespace text and `k` must be between 1 and 20. The key
must allow the collection's embedding model. A successful response has this shape:

```json
{
  "collection": "policies",
  "model_id": "bge-small",
  "chunks": [
    {
      "chunk_id": "a2d4...",
      "text": "Rail travel is allowed for journeys under four hours.",
      "score": 0.173,
      "metadata": {
        "source": "travel-policy.md",
        "page": 3
      }
    }
  ]
}
```

`score` is the raw L2 distance reported by sqlite-vec, so a lower value is a
closer match. Scores from different collections or embedding models should not
be compared. The response includes the `metadata` object exactly as it was
stored, so callers keep source names, page numbers, URLs, or other citation data.

The route returns 401 for a missing or invalid key, 403 when the key does not
allow the collection model, and 404 for an unknown collection. Request
validation failures return 422. A collection without a healthy embedding
replica returns 503. A failed replica call or malformed embedding response
returns 502.

The coordinator uses the first healthy endpoint reported by the registry. One
query makes one embedding call. It does not retry another replica or apply the
gateway's load-balancing policy.

## Open WebUI workspace tool

[Open WebUI workspace tools](https://docs.openwebui.com/features/extensibility/plugin/tools/)
run inside the Open WebUI server and can keep the Fallow key away from the
model. Add the following source under **Workspace > Tools**, then set its valves
and grant the intended users access to it.

```python
"""
title: Fallow RAG
version: 0.1.0
requirements: httpx
"""

import httpx
from pydantic import BaseModel, Field


class Tools:
    class Valves(BaseModel):
        base_url: str = Field(
            default="http://fallow-coordinator:8330",
            description="Fallow coordinator URL",
        )
        api_key: str = Field(
            default="",
            description="Fallow API key",
            json_schema_extra={"input": {"type": "password"}},
        )
        collection: str = Field(
            default="policies",
            description="RAG collection to search",
        )
        result_count: int = Field(default=4, ge=1, le=20)

    def __init__(self) -> None:
        self.valves = self.Valves()

    async def search_fallow(self, query: str) -> dict:
        """Search the configured Fallow collection for passages relevant to query."""
        url = (
            f"{self.valves.base_url.rstrip('/')}"
            f"/v1/rag/collections/{self.valves.collection}/query"
        )
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {self.valves.api_key}"},
                json={"q": query, "k": self.valves.result_count},
            )
            response.raise_for_status()
            return response.json()
```

Use a model with native function calling and attach the tool to that model or
enable it for the current chat. The Open WebUI documentation recommends native
tool calling for supported models.

The password input only masks the key in the interface. To encrypt stored valve
values, follow Open WebUI's
[valve encryption guidance](https://docs.openwebui.com/features/extensibility/plugin/development/valves/#encrypting-valve-values-at-rest)
and set a stable `WEBUI_SECRET_KEY` before enabling encryption.
