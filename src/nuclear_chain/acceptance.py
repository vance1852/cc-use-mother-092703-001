"""核燃料循环批次监管服务的离线贯通验收。

场景：登记来源与成分声明 → 运输交接 → 检测/复检 → 分批 →
清单重复导入（隔离粘性）→ 合批 → 质量放行 → 不可逆处置 →
父批次来源更正（派生影响分析）→ 追溯与哈希链校验。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import BatchService
from .errors import Conflict


COMPOSITION_UF6 = [
    {"element": "U", "isotope": "U-235", "fraction": "0.045"},
    {"element": "U", "isotope": "U-238", "fraction": "0.955"},
]


def declaration(basis_doc: str, supplier: str = "西北铀浓缩厂") -> dict:
    return {
        "source": {"supplier": supplier, "origin": "Y-矿井-2026-09", "document_ref": "SRC-2026-0001"},
        "composition": COMPOSITION_UF6,
        "basis_doc": basis_doc,
    }


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
    service = BatchService(connection, clock)

    for user_id, role in (
        ("reg", "registrar"),
        ("lab", "analyst"),
        ("qa", "quality"),
        ("ship", "logistics"),
        ("rec", "recovery"),
        ("audit", "auditor"),
    ):
        service.create_user(user_id, user_id, role)

    # 1. 登记来源与成分声明
    registered = service.register_batch("reg", {
        "batch_id": "UF6-A01",
        "material_type": "UF6",
        "quantity": "100",
        "unit": "kgU",
        **declaration("供应商质保书 QA-2026-009"),
    })

    # 2. 运输交接：发运与接收
    service.handoff("ship", {
        "batch_id": "UF6-A01", "handoff_type": "ship",
        "from_party": "西北铀浓缩厂", "to_party": "元件制造厂中转库",
        "document_ref": "运单 WAYB-001", "observed_quantity": "100",
        "idempotency_key": "handoff-ship-1",
    })
    clock.advance(hours=12)
    service.handoff("ship", {
        "batch_id": "UF6-A01", "handoff_type": "receive",
        "from_party": "西北铀浓缩厂", "to_party": "元件制造厂中转库",
        "document_ref": "签收单 RECV-001", "observed_quantity": "99.8",
        "idempotency_key": "handoff-recv-1",
    })

    # 3. 初检不合格 → 质量隔离；复检合格后放行
    test1 = service.record_test("lab", "UF6-A01", "test-1", "ICP-MS", "ICP-MS-07",
                                {"u235": "0.041", "impurities_ppm": "310"},
                                "nonconforming", "检测原始记录 TR-001")
    reject = service.decide("qa", "UF6-A01", "reject", "U-235 丰度低于声明值")
    clock.advance(days=1)
    test2 = service.record_test("lab", "UF6-A01", "test-2", "ICP-MS", "ICP-MS-07",
                                {"u235": "0.045", "impurities_ppm": "95"},
                                "conforming", "复检原始记录 TR-002")
    released = service.decide("qa", "UF6-A01", "release", "复检合格，附复检报告 TR-002", test2["test_id"])

    # 4. 分批为两份芯块前驱料
    split = service.split_batch("reg", {
        "parent_id": "UF6-A01", "split_id": "SPLIT-01",
        "idempotency_key": "split-01",
        "parts": [
            {"batch_id": "UF6-A01-A", "quantity": "60"},
            {"batch_id": "UF6-A01-B", "quantity": "39.8"},
        ],
    })
    # A 份检测异常 → 隔离；B 份合格 → 放行
    service.record_test("lab", "UF6-A01-A", "test-a-1", "伽马谱", "GAMMA-02",
                        {"surface_dose": "超标"}, "nonconforming", "TR-A-001")
    service.decide("qa", "UF6-A01-A", "quarantine", "表面剂量异常，待查")
    service.record_test("lab", "UF6-A01-B", "test-b-1", "ICP-MS", "ICP-MS-07",
                        {"u235": "0.045"}, "conforming", "TR-B-001")
    service.decide("qa", "UF6-A01-B", "release", "合格放行")

    # 5. 重复导入外部清单：隔离批次被保留且不复活；其他重复批次冲突回滚
    preserved = service.import_manifest("reg", {
        "idempotency_key": "manifest-002",
        "source_ref": "外部台账 EX-2026-09-29",
        "entries": [
            {
                "batch_id": "UF6-A01-A",
                "material_type": "UF6", "quantity": "60", "unit": "kgU",
                **declaration("外部台账条目（重复导入）"),
            },
            {
                "batch_id": "UF6-NEW-1",
                "material_type": "UF6", "quantity": "10", "unit": "kgU",
                **declaration("外部台账新条目"),
            },
        ],
    })
    # 已放行批次再次出现在清单中 → 冲突（防止重复入库）
    conflict = None
    try:
        service.import_manifest("reg", {
            "idempotency_key": "manifest-003",
            "source_ref": "外部台账 EX-2026-09-29-B",
            "entries": [
                {
                    "batch_id": "UF6-A01-B",
                    "material_type": "UF6", "quantity": "39.8", "unit": "kgU",
                    **declaration("重复的已放行批次"),
                },
            ],
        })
    except Conflict as exc:
        conflict = str(exc)

    # 6. B 份与另一批合批（全部合格 → 新批次待检）
    service.register_batch("reg", {
        "batch_id": "UF6-C02", "material_type": "UF6", "quantity": "20", "unit": "kgU",
        **declaration("供应商质保书 QA-2026-011", supplier="西南转化厂"),
        "idempotency_key": "reg-c02",
    })
    service.record_test("lab", "UF6-C02", "test-c-1", "ICP-MS", "ICP-MS-08",
                        {"u235": "0.044"}, "conforming", "TR-C-001")
    service.decide("qa", "UF6-C02", "release", "合格放行")
    merge = service.merge_batches("reg", {
        "merge_id": "MERGE-01", "idempotency_key": "merge-01",
        "parents": [
            {"batch_id": "UF6-A01-B", "quantity": "39.8"},
            {"batch_id": "UF6-C02", "quantity": "20"},
        ],
        "child": {
            "batch_id": "UO2-M01", "material_type": "UF6", "quantity": "59.8", "unit": "kgU",
            **declaration("合批配料单 BLEND-M01"),
        },
    })
    service.record_test("lab", "UO2-M01", "test-m-1", "ICP-MS", "ICP-MS-07",
                        {"u235": "0.0447"}, "conforming", "TR-M-001")
    service.decide("qa", "UO2-M01", "release", "合批均匀性合格")

    # 7. 隔离的 A 份送资源回收（不可逆处置）
    disposition = service.dispose("rec", {
        "batch_id": "UF6-A01-A", "kind": "recovery", "quantity": "60",
        "method": "湿法回收", "facility": "回收车间 R-1",
        "document_ref": "处置令 DISP-001", "idempotency_key": "disp-001",
    })
    # 处置后任何变更都被拒绝
    blocked = None
    try:
        service.handoff("ship", {
            "batch_id": "UF6-A01-A", "handoff_type": "ship",
            "from_party": "隔离库", "to_party": "任何地方",
            "document_ref": "X", "observed_quantity": "1",
            "idempotency_key": "handoff-after-dispose",
        })
    except Exception as exc:  # noqa: BLE001
        blocked = str(exc)

    # 8. 父批次来源信息更正：保留原始依据，并看清派生影响
    correction = service.correct_declaration(
        "reg", "UF6-A01", 1,
        "矿井台账核对：来源文档编号更正",
        {**declaration("供应商质保书 QA-2026-009（修订 R1：来源矿井编号更正）")},
    )
    impact = service.lineage("audit", "UF6-A01")
    trace = service.trace("audit", "UF6-A01-A")
    chain = service.audit_chain("audit")

    connection.close()
    return {
        "status": "ok",
        "registered": registered,
        "first_reject": reject,
        "released_after_retest": released,
        "split": split,
        "manifest_preserved": preserved,
        "duplicate_conflict": conflict,
        "merge": merge,
        "disposition": disposition,
        "post_disposition_blocked": blocked,
        "correction": correction,
        "lineage": {
            "descendant_count": impact["descendant_count"],
            "descendants": impact["descendants"],
            "disposed_descendants": impact["disposed_descendants"],
            "quarantined_descendants": impact["quarantined_descendants"],
        },
        "trace_a_events": [event["event_type"] for event in trace["events"]],
        "audit_chain": chain,
        "workspace": workspace.name,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行核燃料循环批次监管服务离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
