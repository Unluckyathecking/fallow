"""Typed errors raised by the agent model cache."""


class ModelCacheError(Exception):
    """Base class for all model-cache failures."""


class ModelFetchError(ModelCacheError):
    """A blob could not be downloaded after exhausting the retry budget.

    Raised for transport-level failures (connection reset, timeout) and
    non-resumable HTTP status codes. Distinct from a *content* failure so
    callers can retry a fetch later without treating the model as poisoned.
    """


class ModelVerificationError(ModelCacheError):
    """A fully downloaded blob failed sha256 / size verification.

    Deterministic given the bytes, so it is never retried: the partial file is
    deleted and the caller must decide whether the manifest itself is wrong.
    """
