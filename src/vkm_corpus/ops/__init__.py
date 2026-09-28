"""Operational control plane (PostgreSQL on EDGE): runs, jobs, attempts, workers, heartbeats, errors, review tasks,
service state.

Operational state only (постановка §16, H-42): the pipeline CLI works without it, and losing the database loses no
scientific data. Jobs follow the plan-first lifecycle of H-12 (``vkm_corpus.ops.jobs``).
"""
