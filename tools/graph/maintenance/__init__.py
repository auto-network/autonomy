"""Substrate maintenance verbs.

Long-running cron-driven jobs that operate on the per-org Settings DBs:
cache TTL sweeps, eventually periodic vacuum/reindex/integrity work.
Each verb exposes a thin :func:`main` that the ``graph maintenance``
CLI parser dispatches into.
"""
