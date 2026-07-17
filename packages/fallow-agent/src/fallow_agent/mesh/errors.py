"""Typed errors for the agent's mesh fetch path.

Every mesh failure — a malformed manifest, a bad signature, a missing chunk, an
HTTP error — surfaces as :class:`MeshError` or a modelmesh ``ModelmeshError`` so
the model store can catch one family and fall back to the blob download.
"""


class MeshError(Exception):
    """A mesh fetch could not complete; the caller should fall back to the blob."""
