"""Exceptions raised by the modelmesh library.

All failures that mean "the bytes are not what the manifest promised" derive
from ``VerificationError`` so a caller can reject a bad reconstruction with one
except clause and never trust unverified content.
"""


class ModelmeshError(Exception):
    """Base class for every error this package raises."""


class VerificationError(ModelmeshError):
    """Content did not match the hash or signature that vouches for it."""


class ChunkNotFound(ModelmeshError):
    """A chunk the manifest requires is absent from the store."""
