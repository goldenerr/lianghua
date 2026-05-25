"""Disaster recovery + Kill Switch joint drill (test-009)."""

from quant_trading.operations import BackupManager
from quant_trading.risk.advanced import KillSwitch


def test_backup_restore_flow(tmp_path):
    bm = BackupManager(storage_dir=tmp_path)
    backup_id = bm.backup("clickhouse")
    assert isinstance(backup_id, str) and backup_id
    assert bm.last_backup == backup_id
    assert bm.restore(backup_id) is True
    verify = bm.verify()
    assert verify["passed"] is True
    assert verify["rto_minutes"] <= 120
    assert verify["rpo_minutes"] <= 60


def test_kill_switch_activation():
    ks = KillSwitch()
    assert ks.is_active is False
    ks.activate("drill")
    assert ks.is_active is True
    ks.deactivate()
    assert ks.is_active is False
