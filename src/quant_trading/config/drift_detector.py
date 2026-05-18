
"""Multi-environment config drift detection (config-drift-001). AGENTS.md §46: CI/CD config drift check."""
import yaml
def detect_drift(dev_config: str, prod_config: str) -> dict:
    d, p = yaml.safe_load(open(dev_config)), yaml.safe_load(open(prod_config)) if dev_config and prod_config else ({},{})
    diffs = {k: (d.get(k), p.get(k)) for k in set(d)|set(p) if d.get(k) != p.get(k)}
    return {"drifting_keys": list(diffs.keys()), "count": len(diffs)}
