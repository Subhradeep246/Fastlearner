"""Versioned application commands (seeds and other one-off operations).

Data seeds are versioned application commands rather than irreversible
migration side effects, so they can be applied idempotently after migrations.
"""
