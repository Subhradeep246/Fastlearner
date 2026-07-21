"""Authentication and authorization adapters.

Holds secure session token contracts, the identity provider abstraction, the
local development identity provider used by loopback-only dev sessions, and the
centralized authorization policy engine every application service consults.
"""

from app.auth.policy import (
    AccessMode,
    DenialSink,
    LoggingDenialSink,
    NullDenialSink,
    PolicyDenial,
    PolicyEngine,
    ResourceKind,
    pseudonymize,
)

__all__ = [
    "AccessMode",
    "DenialSink",
    "LoggingDenialSink",
    "NullDenialSink",
    "PolicyDenial",
    "PolicyEngine",
    "ResourceKind",
    "pseudonymize",
]
