"""Full-chain capital impact assessment automation (test-007)."""

import json

from quant_trading.capital_impact_v59 import (
    CapitalImpactAssessment,
    generate_v59_report,
    requires_risk_approval,
)


def test_impact_assessment_generated(tmp_path):
    report = generate_v59_report(output_dir=tmp_path)
    json_path = tmp_path / "capital_impact_v5.9.json"
    md_path = tmp_path / "capital_impact_v5.9.md"

    assert json_path.exists()
    assert md_path.exists()
    assert report["strategy"]
    loaded = json.loads(json_path.read_text(encoding="utf-8"))
    assert loaded["backtest"]["sharpe"] >= 1.2


def test_risk_approval_required():
    a = CapitalImpactAssessment(approved=False)
    assert requires_risk_approval(a) is True
    a2 = CapitalImpactAssessment(approved=True, approved_by="risk_officer")
    assert requires_risk_approval(a2) is False
