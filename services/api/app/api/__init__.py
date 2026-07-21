"""HTTP API layer: routers, dependencies, error envelopes, and serialization.

This package composes the authenticated ``/v1`` surface on top of the
application services. It owns request identifiers, authentication dependencies,
typed error envelopes with safe messages, deterministic time/UUID/cursor
serialization, and write idempotency enforcement (Requirement 17).
"""
