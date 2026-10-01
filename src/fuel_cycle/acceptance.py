"""核燃料循环批次监管的完整离线验收流程。

覆盖：来源与成分声明登记、重复导入防复活、检测复核、四眼放行、
分批、父信息更正与派生批次影响分析、复检再放行、不可逆处置、
运输交接、合批（资源回收）与全局审计哈希链校验。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict
from .service import FuelCycleService
from .storage import connect, inspect_schema


LIMITS = {
    "u3o8_fraction": {"min": "0.950"},
    "impurity_ppm": {"max": "500"},
}


def _pass_results() -> dict[str, str]:
    return {"u3o8_fraction": "0.996", "impurity_ppm": "118"}


def _declaration(batch_id: str, reference: str, quantity: str) -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "source_type": "mining",
        "source_reference": reference,
        "supplier": "南方铀业",
        "origin_doc_ref": f"ORIGIN-{reference}",
        "material_type": "U3O8",
        "quantity": quantity,
        "unit": "kg",
        "components": [
            {"element": "U", "weight_fraction": "0.848"},
            {"element": "O", "weight_fraction": "0.150"},
            {"element": "impurities", "weight_fraction": "0.002"},
        ],
        "received_at": "2026-09-28T02:00:00Z",
    }


def _inspect_and_release(service: FuelCycleService, batch_id: str, analyst: str = "analyst-1") -> int:
    inspection = service.record_inspection(
        analyst, batch_id, "成分丰度复检", "中心分析实验室", "GB-TECH-2026",
        _pass_results(), LIMITS,
    )
    service.record_decision("qa-1", batch_id, "release", "检测合格，准予放行", inspection["test_id"])
    return inspection["test_id"]


def run(workspace: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="fuel-cycle-") as temporary:
        database = Path(temporary) / "fuel_cycle.sqlite3"
        connection = connect(database)
        try:
            service = FuelCycleService(
                connection,
                FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)),
            )
            for user_id, name, role in (
                ("op-1", "物料操作员", "operator"),
                ("analyst-1", "分析员甲", "analyst"),
                ("analyst-2", "分析员乙", "analyst"),
                ("qa-1", "质量负责人", "quality"),
                ("custodian-1", "发货保管员", "custodian"),
                ("custodian-2", "接收保管员", "custodian"),
                ("auditor-1", "审计人员", "auditor"),
            ):
                service.create_user(user_id, name, role)

            # 1) 来源与成分声明登记。
            service.register_declaration("op-1", _declaration("U3O8-001", "MINE-2026-0001", "100"), "imp-001")
            service.register_declaration("op-1", _declaration("U3O8-002", "MINE-2026-0002", "50"), "imp-002")

            # 2) 重复导入不得让已隔离材料复活。
            service.register_declaration("op-1", _declaration("U3O8-003", "MINE-2026-0003", "10"), "imp-003")
            service.record_decision("qa-1", "U3O8-003", "quarantine", "到厂复核封记异常，先行隔离")
            replay = service.register_declaration(
                "op-1", _declaration("U3O8-003", "MINE-2026-0003", "10"), "imp-003"
            )
            quarantined = service.get_batch("U3O8-003")
            if quarantined["status"] != "quarantined":
                raise RuntimeError("重复导入不应改变已隔离批次的状态")
            try:
                service.register_declaration(
                    "op-1", _declaration("U3O8-003-X", "MINE-2026-0003", "10"), "imp-003b"
                )
            except Conflict:
                duplicate_blocked = True
            else:
                duplicate_blocked = False
            if not duplicate_blocked:
                raise RuntimeError("同一来源凭证的重复导入必须被拒绝")

            # 3) 检测复核与四眼放行。
            _inspect_and_release(service, "U3O8-001")
            _inspect_and_release(service, "U3O8-002")

            # 4) 分批：父批次耗尽，两个派生批次需重新检测放行。
            service.split_batch(
                "op-1", "split-001", "U3O8-001",
                [{"batch_id": "U3O8-001-A", "quantity": "60"},
                 {"batch_id": "U3O8-001-B", "quantity": "40"}],
                note="按运输与回收用途分批",
            )
            _inspect_and_release(service, "U3O8-001-A")
            _inspect_and_release(service, "U3O8-001-B")

            # 5) 父批次来源信息更正（补正供货单据），旧版本原样保留。
            service.clock.advance(hours=2)
            corrected = _declaration("U3O8-001", "MINE-2026-0001", "100")
            corrected["origin_doc_ref"] = "ORIGIN-MINE-2026-0001-REV2"
            corrected["components"] = [
                {"element": "U", "weight_fraction": "0.845"},
                {"element": "O", "weight_fraction": "0.150"},
                {"element": "impurities", "weight_fraction": "0.005"},
            ]
            history = service.correct_declaration("qa-1", "U3O8-001", corrected)
            impact_before = service.impact_analysis("auditor-1", "U3O8-001")
            if impact_before["affected_count"] != 2:
                raise RuntimeError("更正后两个派生批次都应标记为受影响")

            # 6) A 依据更正后复检重新放行；B 维持隔离并进入不可逆处置。
            service.clock.advance(hours=1)
            _inspect_and_release(service, "U3O8-001-A", analyst="analyst-2")
            service.record_decision("qa-1", "U3O8-001-B", "quarantine", "父批次成分更正，暂停使用等待处置")
            disposal = service.dispose_batch(
                "qa-1", "U3O8-001-B", "稳定化固化处置", "WASTE-AUTH-77",
                "父批次更正后杂质高于内控值，按废物处置", witness="auditor-1",
            )
            impact_after = service.impact_analysis("auditor-1", "U3O8-001")
            statuses = {item["batch_id"]: item for item in impact_after["descendants"]}
            if not statuses["U3O8-001-A"]["revalidated_after_correction"]:
                raise RuntimeError("A 应已在更正后复检并重新放行")
            if not statuses["U3O8-001-B"]["irreversible"]:
                raise RuntimeError("B 应被识别为已进入不可逆处置")

            # 7) A 经运输交接后仍然可用（在途期间状态冻结）。
            service.dispatch_transfer(
                "custodian-1", "tr-001", "U3O8-001-A",
                "精炼厂", "回收车间", "厂区一号库", "回收车间收料口",
                manifest_ref="MANIFEST-TR-001",
            )
            if service.get_batch("U3O8-001-A")["status"] != "in_transit":
                raise RuntimeError("发运后批次应处于在途状态")
            service.receive_transfer("custodian-2", "tr-001", "包装与封记完好")

            # 8) 合批（资源回收）：A 与独立来源的 U3O8-002 合批后重新检测放行。
            service.merge_batches(
                "op-1", "merge-001", ["U3O8-001-A", "U3O8-002"],
                [{"batch_id": "RECOVERED-001", "quantity": "110"}],
                note="回收资源集中再加工",
            )
            _inspect_and_release(service, "RECOVERED-001", analyst="analyst-2")

            # 9) 追溯与审计。
            trace = service.trace_batch("auditor-1", "U3O8-001")
            chain = service.verify_chain("auditor-1")
            schema = inspect_schema(connection)
            recovered_status = service.get_batch("RECOVERED-001")["status"]
        finally:
            connection.close()
    if len(history) != 2 or history[0]["basis_sha256"] == history[1]["basis_sha256"]:
        raise RuntimeError("更正必须只追加新版本且保留各自原始依据摘要")
    if not chain["valid"]:
        raise RuntimeError("审计哈希链校验失败")
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "declaration_versions": len(history),
        "duplicate_import_blocked": duplicate_blocked,
        "quarantine_survives_replay": quarantined["status"] == "quarantined",
        "replay_response_reused": replay["batch_id"] == "U3O8-003",
        "impact_affected_after_correction": impact_after["affected_count"],
        "impact_irreversible": impact_after["irreversible_count"],
        "disposal_id": disposal["disposal_id"],
        "recovered_batch": recovered_status,
        "trace_event_count": len(trace["events"]),
        "trace_children": sorted(item["batch_id"] for item in trace["children"]),
        "audit_event_count": chain["event_count"],
        "chain_valid": chain["valid"],
        "schema": schema,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行核燃料循环批次监管服务离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
