# Codex Runbook v2

1. Verify inputs.
2. Create a git branch for one phase.
3. Produce an execution plan.
4. Work from clean output directories.
5. Run unit, integration and full-pipeline checks.
6. Save configs, predictions, metrics, logs, environment and hashes.
7. Compare against B1/B6.
8. Stop at failed gates.
9. Commit only reproducible outputs or manifests, not transient caches or model weights unless explicitly required.
