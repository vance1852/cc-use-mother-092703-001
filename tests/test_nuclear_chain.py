from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from nuclear_chain.clock import FrozenClock
from nuclear_chain.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from nuclear_chain.service import BatchService


COMPOSITION = [
    {"element": "U", "isotope": "U-235", "fraction": "0.04"},
    {"element": "U", "isotope": "U-238", "fraction": "0.96"},
]


def decl(basis_doc: str = "质保书 QA-1", supplier: str = "西北铀浓缩厂") -> dict:
    return {
        "source": {"supplier": supplier, "origin": "矿井 Y-2", "document_ref": "SRC-1"},
        "composition": COMPOSITION,
        "basis_doc": basis_doc,
    }


def batch_payload(batch_id: str = "B-1", quantity: str = "100", **overrides) -> dict:
    payload = {
        "batch_id": batch_id,
        "material_type": "UF6",
        "quantity": quantity,
        "unit": "kgU",
        **decl(),
    }
    payload.update(overrides)
    return payload


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        self.service = BatchService(self.connection, self.clock)
        for user_id, role in (
            ("reg", "registrar"),
            ("lab", "analyst"),
            ("qa", "quality"),
            ("ship", "logistics"),
            ("rec", "recovery"),
            ("audit", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    # ------------------------------------------------------------ 登记与校验

    def test_register_and_get_batch(self) -> None:
        result = self.service.register_batch("reg", batch_payload())
        self.assertEqual(result["status"], "registered")
        batch = self.service.get_batch("B-1")
        self.assertEqual(batch["quantity_text"], "100")
        self.assertEqual(batch["remaining_quantity_text"], "100")
        self.assertEqual(batch["revision"], 1)

    def test_composition_fractions_must_sum_to_one(self) -> None:
        payload = batch_payload(composition=[
            {"element": "U", "isotope": "U-235", "fraction": "0.04"},
            {"element": "U", "isotope": "U-238", "fraction": "0.90"},
        ])
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("reg", payload)

    def test_duplicate_composition_rejected(self) -> None:
        payload = batch_payload(composition=[
            {"element": "U", "isotope": "U-235", "fraction": "0.5"},
            {"element": "U", "isotope": "U-235", "fraction": "0.5"},
        ])
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("reg", payload)

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_batch("lab", batch_payload())
        self.service.register_batch("reg", batch_payload())
        with self.assertRaises(Forbidden):
            self.service.record_test("qa", "B-1", "t1", "m", "i", {"v": 1}, "conforming", "d")
        with self.assertRaises(Forbidden):
            self.service.decide("lab", "B-1", "release", "ok")
        with self.assertRaises(Forbidden):
            self.service.audit_chain("reg")

    def test_unknown_user(self) -> None:
        with self.assertRaises(NotFound):
            self.service.register_batch("nobody", batch_payload())

    # ------------------------------------------------------------ 声明更正

    def test_correction_keeps_original_declaration(self) -> None:
        self.service.register_batch("reg", batch_payload())
        corrected = self.service.correct_declaration(
            "reg", "B-1", 1, "来源矿井编号更正", decl("质保书 QA-1（修订 R1）")
        )
        self.assertEqual(corrected["revision"], 2)
        trace = self.service.trace("audit", "B-1")
        self.assertEqual([d["revision_no"] for d in trace["declarations"]], [1, 2])
        self.assertIsNone(trace["declarations"][0]["supersedes_id"])
        self.assertEqual(
            trace["declarations"][1]["supersedes_id"], trace["declarations"][0]["declaration_id"]
        )
        self.assertEqual(trace["batch"]["revision"], 2)
        self.assertEqual(trace["declarations"][0]["basis_doc"], "质保书 QA-1")

    def test_correction_requires_current_revision(self) -> None:
        self.service.register_batch("reg", batch_payload())
        self.service.correct_declaration("reg", "B-1", 1, "r", decl("修订 R1"))
        with self.assertRaises(InvalidState):
            self.service.correct_declaration("reg", "B-1", 1, "再次", decl("修订 R2"))

    def test_correction_requires_reason(self) -> None:
        self.service.register_batch("reg", batch_payload())
        with self.assertRaises(ValidationFailed):
            self.service.correct_declaration("reg", "B-1", 1, "  ", decl("修订 R1"))

    # ------------------------------------------------------------ 检测与放行

    def _conforming_path(self, batch_id: str = "B-1") -> int:
        self.service.register_batch("reg", batch_payload(batch_id))
        test = self.service.record_test(
            "lab", batch_id, "t-1", "ICP-MS", "ICP-1", {"u235": "0.04"},
            "conforming", "记录 TR-1",
        )
        return test["test_id"]

    def test_release_requires_conforming_test(self) -> None:
        self.service.register_batch("reg", batch_payload())
        self.service.record_test("lab", "B-1", "t-1", "m", "i", {"v": 1},
                                 "nonconforming", "TR-1")
        with self.assertRaises(InvalidState):
            self.service.decide("qa", "B-1", "release", "无合格依据也要放行")

    def test_release_after_conforming_test(self) -> None:
        test_id = self._conforming_path()
        result = self.service.decide("qa", "B-1", "release", "合格", test_id)
        self.assertEqual(result["status"], "released")

    def test_quarantined_batch_requires_fresh_retest_before_release(self) -> None:
        self.service.register_batch("reg", batch_payload())
        first = self.service.record_test("lab", "B-1", "t-1", "m", "i", {"v": 1},
                                         "conforming", "TR-1")
        self.service.decide("qa", "B-1", "quarantine", "运输异常，隔离待查")
        # 旧的合格检测不能作为放行依据
        with self.assertRaises(InvalidState):
            self.service.decide("qa", "B-1", "release", "凭旧检测放行", first["test_id"])
        self.clock.advance(days=2)
        self.service.record_test("lab", "B-1", "t-2", "m", "i", {"v": 2},
                                 "conforming", "复检 TR-2")
        result = self.service.decide("qa", "B-1", "release", "复检合格放行")
        self.assertEqual(result["status"], "released")

    def test_reject_freezes_batch_into_quarantine(self) -> None:
        self.service.register_batch("reg", batch_payload())
        result = self.service.decide("qa", "B-1", "reject", "拒收")
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.service.get_batch("B-1")["status"], "quarantined")

    def test_test_idempotent_replay_and_conflict(self) -> None:
        self.service.register_batch("reg", batch_payload())
        payload_args = ("B-1", "t-1", "ICP-MS", "ICP-1", {"u235": "0.04"}, "conforming", "TR-1")
        first = self.service.record_test("lab", *payload_args)
        second = self.service.record_test("lab", *payload_args)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["test_id"], second["test_id"])
        with self.assertRaises(Conflict):
            self.service.record_test(
                "lab", "B-1", "t-1", "ICP-MS", "ICP-1", {"u235": "0.05"},
                "conforming", "TR-1",
            )
        count = self.connection.execute("SELECT count(*) FROM tests").fetchone()[0]
        self.assertEqual(count, 1)

    # ------------------------------------------------------------ 交接

    def test_handoff_recorded_and_over_remaining_rejected(self) -> None:
        self.service.register_batch("reg", batch_payload())
        result = self.service.handoff("ship", {
            "batch_id": "B-1", "handoff_type": "ship",
            "from_party": "厂 A", "to_party": "库 B", "document_ref": "W-1",
            "observed_quantity": "100", "idempotency_key": "h-1",
        })
        self.assertIn("custody_id", result)
        with self.assertRaises(ValidationFailed):
            self.service.handoff("ship", {
                "batch_id": "B-1", "handoff_type": "receive",
                "from_party": "厂 A", "to_party": "库 B", "document_ref": "W-2",
                "observed_quantity": "101", "idempotency_key": "h-2",
            })

    # ------------------------------------------------------------ 分批合批

    def test_split_checks_quantity_balance(self) -> None:
        self.service.register_batch("reg", batch_payload())
        with self.assertRaises(ValidationFailed):
            self.service.split_batch("reg", {
                "parent_id": "B-1", "split_id": "S-1", "idempotency_key": "s-1",
                "parts": [
                    {"batch_id": "B-1-A", "quantity": "60"},
                    {"batch_id": "B-1-B", "quantity": "41"},
                ],
            })

    def test_split_inherits_declaration_and_quarantine(self) -> None:
        self.service.register_batch("reg", batch_payload())
        self.service.record_test("lab", "B-1", "t-1", "m", "i", {"v": 1},
                                 "nonconforming", "TR-1")
        self.service.decide("qa", "B-1", "quarantine", "隔离")
        result = self.service.split_batch("reg", {
            "parent_id": "B-1", "split_id": "S-1", "idempotency_key": "s-1",
            "parts": [
                {"batch_id": "B-1-A", "quantity": "60"},
                {"batch_id": "B-1-B", "quantity": "40"},
            ],
        })
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.service.get_batch("B-1-A")["status"], "quarantined")
        # 隔离中诞生的子批次：没有新的复检合格结论不能放行
        with self.assertRaises(InvalidState):
            self.service.decide("qa", "B-1-A", "release", "直接放行")
        # 子批次保留来源与成分的继承依据
        trace = self.service.trace("audit", "B-1-A")
        self.assertEqual(len(trace["declarations"]), 1)
        self.assertIn("继承自批次", trace["declarations"][0]["basis_doc"])
        self.assertEqual(trace["parents"][0]["parent_id"], "B-1")
        # 父批次结余扣减
        self.assertEqual(self.service.get_batch("B-1")["remaining_quantity_text"], "0")

    def test_split_idempotent_replay(self) -> None:
        self.service.register_batch("reg", batch_payload())
        raw = {
            "parent_id": "B-1", "split_id": "S-1", "idempotency_key": "s-1",
            "parts": [
                {"batch_id": "B-1-A", "quantity": "60"},
                {"batch_id": "B-1-B", "quantity": "40"},
            ],
        }
        first = self.service.split_batch("reg", raw)
        second = self.service.split_batch("reg", raw)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["child_ids"], second["child_ids"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM batches").fetchone()[0], 3
        )

    def test_merge_requires_quantity_conservation_and_unit_match(self) -> None:
        self.service.register_batch("reg", batch_payload("B-1", "60"))
        self.service.register_batch("reg", batch_payload("B-2", "40"))
        base_child = {
            "batch_id": "M-1", "material_type": "UF6", "quantity": "99", "unit": "kgU",
            **decl("配料单"),
        }
        with self.assertRaises(ValidationFailed):
            self.service.merge_batches("reg", {
                "merge_id": "MG-1", "idempotency_key": "m-1",
                "parents": [
                    {"batch_id": "B-1", "quantity": "60"},
                    {"batch_id": "B-2", "quantity": "40"},
                ],
                "child": base_child,
            })
        bad_unit = dict(base_child, quantity="100", unit="gU")
        with self.assertRaises(ValidationFailed):
            self.service.merge_batches("reg", {
                "merge_id": "MG-1", "idempotency_key": "m-1",
                "parents": [
                    {"batch_id": "B-1", "quantity": "60"},
                    {"batch_id": "B-2", "quantity": "40"},
                ],
                "child": bad_unit,
            })

    def test_merge_inherits_quarantine_and_conserves_quantity(self) -> None:
        self._released_batch("B-1", "60")
        self.service.register_batch("reg", batch_payload("B-2", "40"))
        self.service.record_test("lab", "B-2", "t-b", "m", "i", {"v": 1},
                                 "nonconforming", "TR")
        self.service.decide("qa", "B-2", "quarantine", "隔离")
        result = self.service.merge_batches("reg", {
            "merge_id": "MG-1", "idempotency_key": "m-1",
            "parents": [
                {"batch_id": "B-1", "quantity": "60"},
                {"batch_id": "B-2", "quantity": "40"},
            ],
            "child": {
                "batch_id": "M-1", "material_type": "UF6",
                "quantity": "100", "unit": "kgU", **decl("配料单"),
            },
        })
        self.assertEqual(result["status"], "quarantined")
        self.assertEqual(self.service.get_batch("B-1")["remaining_quantity_text"], "0")
        self.assertEqual(self.service.get_batch("B-2")["remaining_quantity_text"], "0")
        lineage = self.service.lineage("audit", "M-1")
        self.assertEqual({a["batch_id"] for a in lineage["ancestors"]}, {"B-1", "B-2"})

    def test_merge_rejects_disposed_parent(self) -> None:
        self._released_batch("B-1", "60")
        self._released_batch("B-2", "40")
        self.service.dispose("rec", {
            "batch_id": "B-2", "kind": "discard", "quantity": "40",
            "method": "整批弃置", "facility": "F", "document_ref": "D",
            "idempotency_key": "disp-1",
        })
        with self.assertRaises(InvalidState):
            self.service.merge_batches("reg", {
                "merge_id": "MG-1", "idempotency_key": "m-1",
                "parents": [
                    {"batch_id": "B-1", "quantity": "60"},
                    {"batch_id": "B-2", "quantity": "40"},
                ],
                "child": {
                    "batch_id": "M-1", "material_type": "UF6",
                    "quantity": "100", "unit": "kgU", **decl("配料单"),
                },
            })

    def _released_batch(self, batch_id: str, quantity: str) -> None:
        self.service.register_batch("reg", batch_payload(batch_id, quantity))
        self.service.record_test("lab", batch_id, f"t-{batch_id}", "m", "i", {"v": 1},
                                 "conforming", "TR")
        self.service.decide("qa", batch_id, "release", "合格")

    # ------------------------------------------------------------ 不可逆处置

    def test_dispose_is_terminal(self) -> None:
        self.service.register_batch("reg", batch_payload())
        self.service.dispose("rec", {
            "batch_id": "B-1", "kind": "final_storage", "quantity": "100",
            "method": "固化处置", "facility": "F-1", "document_ref": "D-1",
            "idempotency_key": "d-1",
        })
        batch = self.service.get_batch("B-1")
        self.assertEqual(batch["status"], "disposed")
        self.assertEqual(batch["disposition_kind"], "final_storage")
        with self.assertRaises(InvalidState):
            self.service.record_test("lab", "B-1", "t-1", "m", "i", {"v": 1},
                                     "conforming", "TR")
        with self.assertRaises(InvalidState):
            self.service.handoff("ship", {
                "batch_id": "B-1", "handoff_type": "ship",
                "from_party": "a", "to_party": "b", "document_ref": "d",
                "observed_quantity": "1", "idempotency_key": "h-1",
            })
        with self.assertRaises(InvalidState):
            self.service.correct_declaration("reg", "B-1", 1, "r", decl("修订"))

    def test_dispose_must_cover_full_remaining_quantity(self) -> None:
        self.service.register_batch("reg", batch_payload())
        with self.assertRaises(ValidationFailed):
            self.service.dispose("rec", {
                "batch_id": "B-1", "kind": "recovery", "quantity": "99",
                "method": "回收", "facility": "F", "document_ref": "D",
                "idempotency_key": "d-1",
            })

    # ------------------------------------------------------------ 清单导入

    def _manifest(self, key: str, entries: list[dict], source_ref: str = "EXT-1") -> dict:
        return {"idempotency_key": key, "source_ref": source_ref, "entries": entries}

    def test_manifest_import_registers_batches(self) -> None:
        result = self.service.import_manifest("reg", self._manifest("mf-1", [
            batch_payload("M-A", "10"),
            batch_payload("M-B", "20"),
        ]))
        self.assertEqual(result["registered_count"], 2)
        self.assertEqual(result["preserved_quarantine_count"], 0)

    def test_manifest_duplicate_rolls_back_entire_import(self) -> None:
        self.service.register_batch("reg", batch_payload("M-A", "10"))
        with self.assertRaises(Conflict):
            self.service.import_manifest("reg", self._manifest("mf-1", [
                batch_payload("M-NEW", "5"),
                batch_payload("M-A", "10"),
            ]))
        # 同批次内先登记成功的条目也随事务回滚
        self.assertIsNone(
            self.connection.execute("SELECT 1 FROM batches WHERE batch_id='M-NEW'").fetchone()
        )

    def test_quarantined_batch_not_revived_by_reimport(self) -> None:
        self.service.register_batch("reg", batch_payload("M-A", "10"))
        self.service.record_test("lab", "M-A", "t-1", "m", "i", {"v": 1},
                                 "nonconforming", "TR")
        self.service.decide("qa", "M-A", "quarantine", "隔离")
        result = self.service.import_manifest("reg", self._manifest("mf-1", [
            {
                "batch_id": "M-A", "material_type": "UF6", "quantity": "10",
                "unit": "kgU", **decl("外部清单中的重复条目"),
            },
            batch_payload("M-B", "20"),
        ]))
        self.assertEqual(result["preserved_quarantine"], ["M-A"])
        self.assertEqual(result["registered"], ["M-B"])
        self.assertEqual(self.service.get_batch("M-A")["status"], "quarantined")
        trace = self.service.trace("audit", "M-A")
        self.assertIn("manifest.quarantine_preserved", [e["event_type"] for e in trace["events"]])

    def test_manifest_idempotent_replay(self) -> None:
        payload = self._manifest("mf-1", [batch_payload("M-A", "10")])
        first = self.service.import_manifest("reg", payload)
        second = self.service.import_manifest("reg", payload)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["import_id"], second["import_id"])

    # ------------------------------------------------------------ 追溯与谱系

    def test_lineage_flags_impacted_and_disposed_descendants(self) -> None:
        # B-1 分成 A、B；A 隔离后处置；B 与 C 合批成 M
        self._released_batch("B-1", "100")
        self.service.split_batch("reg", {
            "parent_id": "B-1", "split_id": "S-1", "idempotency_key": "s-1",
            "parts": [
                {"batch_id": "B-1-A", "quantity": "60"},
                {"batch_id": "B-1-B", "quantity": "40"},
            ],
        })
        self.service.record_test("lab", "B-1-A", "t-a", "m", "i", {"v": 1},
                                 "nonconforming", "TR")
        self.service.decide("qa", "B-1-A", "quarantine", "隔离")
        self.service.dispose("rec", {
            "batch_id": "B-1-A", "kind": "recovery", "quantity": "60",
            "method": "回收", "facility": "F", "document_ref": "D",
            "idempotency_key": "d-a",
        })
        self._released_batch("C-2", "10")
        self.service.merge_batches("reg", {
            "merge_id": "MG-1", "idempotency_key": "m-1",
            "parents": [
                {"batch_id": "B-1-B", "quantity": "40"},
                {"batch_id": "C-2", "quantity": "10"},
            ],
            "child": {
                "batch_id": "M-1", "material_type": "UF6",
                "quantity": "50", "unit": "kgU", **decl("配料单"),
            },
        })
        self.service.correct_declaration("reg", "B-1", 1, "来源更正", decl("修订 R1"))
        lineage = self.service.lineage("audit", "B-1")
        descendants = {item["batch_id"]: item for item in lineage["descendants"]}
        self.assertEqual(set(descendants), {"B-1-A", "B-1-B", "M-1"})
        self.assertEqual(lineage["disposed_descendants"], ["B-1-A"])
        self.assertEqual(descendants["M-1"]["depth"], 2)
        correction = self.service.trace("audit", "B-1")["events"][-1]
        self.assertEqual(correction["event_type"], "declaration.corrected")
        self.assertEqual(
            set(correction["payload"]["impacted_descendants"]),
            {"B-1-A", "B-1-B", "M-1"},
        )
        self.assertEqual(correction["payload"]["disposed_descendants"], ["B-1-A"])

    def test_trace_lists_every_handoff_and_decision(self) -> None:
        self.service.register_batch("reg", batch_payload())
        self.service.handoff("ship", {
            "batch_id": "B-1", "handoff_type": "ship",
            "from_party": "a", "to_party": "b", "document_ref": "W-1",
            "observed_quantity": "100", "idempotency_key": "h-1",
        })
        test_id = self._add_conforming_test("B-1", "t-1")
        self.service.decide("qa", "B-1", "release", "合格", test_id)
        trace = self.service.trace("audit", "B-1")
        self.assertEqual(len(trace["custody"]), 1)
        self.assertEqual(trace["custody"][0]["document_ref"], "W-1")
        self.assertEqual([d["decision"] for d in trace["decisions"]], ["release"])
        self.assertEqual(trace["tests"][0]["results"], {"u235": "0.04"})
        event_types = [event["event_type"] for event in trace["events"]]
        self.assertEqual(
            event_types,
            ["batch.registered", "custody.handoff", "test.recorded", "decision.recorded"],
        )

    def _add_conforming_test(self, batch_id: str, key: str) -> int:
        return self.service.record_test(
            "lab", batch_id, key, "m", "i", {"u235": "0.04"}, "conforming", "TR"
        )["test_id"]

    def test_audit_chain_is_valid(self) -> None:
        self.service.register_batch("reg", batch_payload())
        self.service.handoff("ship", {
            "batch_id": "B-1", "handoff_type": "ship",
            "from_party": "a", "to_party": "b", "document_ref": "W-1",
            "observed_quantity": "100", "idempotency_key": "h-1",
        })
        chain = self.service.audit_chain("audit")
        self.assertTrue(chain["valid"])
        self.assertEqual(chain["events"], 2)

    def test_trace_unknown_batch(self) -> None:
        with self.assertRaises(NotFound):
            self.service.trace("audit", "MISSING")


if __name__ == "__main__":
    unittest.main()
