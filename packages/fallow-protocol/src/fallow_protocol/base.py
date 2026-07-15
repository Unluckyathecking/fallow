"""Shared base model for all Fallow wire types.

Every wire type is frozen (immutable after construction) and rejects unknown
fields, so protocol drift between agent and coordinator fails loudly at the
boundary instead of being silently ignored.
"""

from pydantic import BaseModel, ConfigDict


class FallowModel(BaseModel):
    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        protected_namespaces=(),  # allow field names like model_id
    )
