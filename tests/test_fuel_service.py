from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from fuel_cycle.clock import FrozenClock
from fuel_cycle.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from fuel_cycle.service import FuelCycleService


def declaration(batch_id: str = "B-1", reference: str = "REF-1", quantity: str = "100") -> dict[str, object]:
    return {
        "batch_id": batch_id,
        "source_type": "mining",
        "source_reference": reference,
        "supplier": "南方铀业",
        "origin_doc_ref": "ORIGIN-1",
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


LIMITS = {"purity": {"min": "0.95"}, "impurity_ppm": {"max": "500"}}
PASS_RESULTS = {"purity": "0.996", "impurity_ppm": "120"}
FAIL_RESULTS = {"purity": "0.900", "impurity_ppm": "120"}


class FuelServiceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = FuelCycleService(self.connection, self.clock)
        for user_id, role in (
            ("op", "operator"),
            ("analyst", "analyst"),
            ("analyst2", "analyst"),
            ("qa", "quality"),
            ("custodian", "custodian"),
            ("custodian2", "custodian"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)

    def tearDown(self) -> None:
        self.connection.close()

    def register(self, batch_id: str = "B-1", reference: str = "REF-1", quantity: str = "100",
                 source_type: str = "mining", key: str | None = None) -> dict:
        raw = declaration(batch_id, reference, quantity)
        raw["source_type"] = source_type
        return self.service.register_declaration("op", raw, key or f"key-{batch_id}")

    def inspect(self, batch_id: str = "B-1", results=None, limits=None, actor: str = "analyst",
                verdict: str | None = None) -> dict:
        return self.service.record_inspection(
            actor, batch_id, "成分复检", "中心实验室", "M-1",
            results if results is not None else PASS_RESULTS,
            LIMITS if limits is None else limits, verdict=verdict,
        )

    def release(self, batch_id: str = "B-1", test_id: int | None = None, actor: str = "qa") -> dict:
        return self.service.record_decision(
            actor, batch_id, "release", "合格放行", test_id,
        )

    def inspect_and_release(self, batch_id: str = "B-1", analyst: str = "analyst") -> dict:
        test = self.inspect(batch_id, actor=analyst)
        self.release(batch_id, test["test_id"])
        return test


class DeclarationTests(FuelServiceTestBase):
    def test_register_creates_declared_batch(self) -> None:
        result = self.register()
        self.assertEqual(result["status"], "declared")
        batch = self.service.get_batch("B-1")
        self.assertEqual(batch["quantity"], "100")
        history = self.service.get_declaration_history("auditor", "B-1")
        self.assertEqual(len(history), 1)
        self.assertEqual(len(history[0]["basis_sha256"]), 64)

    def test_unknown_role_rejected(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_user("x", "x", "inspector")

    def test_role_permissions_enforced(self) -> None:
        self.register()
        with self.assertRaises(Forbidden):
            self.service.record_decision("op", "B-1", "release", "无权")
        with self.assertRaises(Forbidden):
            self.service.register_declaration("analyst", declaration(), "k")
        with self.assertRaises(Forbidden):
            self.service.correct_declaration("op", "B-1", declaration())

    def test_invalid_components(self) -> None:
        raw = declaration()
        raw["components"] = [{"element": "U", "weight_fraction": "1.5"}]
        with self.assertRaises(ValidationFailed):
            self.service.register_declaration("op", raw, "k-bad")

    def test_idempotent_replay_returns_same_response(self) -> None:
        first = self.register()
        second = self.register()
        self.assertEqual(first, second)
        count = self.connection.execute("SELECT count(*) FROM batches").fetchone()[0]
        self.assertEqual(count, 1)

    def test_same_idempotency_key_different_body_conflicts(self) -> None:
        self.register()
        raw = declaration("B-1", "REF-1", "200")
        with self.assertRaises(Conflict):
            self.service.register_declaration("op", raw, "key-B-1")

    def test_duplicate_source_reference_conflicts_even_for_new_batch_id(self) -> None:
        self.register("B-1", "REF-1")
        with self.assertRaises(Conflict):
            self.register("B-2", "REF-1", key="key-B-2")
        self.assertIsNone(self.connection.execute(
            "SELECT batch_id FROM batches WHERE batch_id='B-2'"
        ).fetchone())

    def test_replay_after_quarantine_does_not_revive(self) -> None:
        """已隔离材料因重复导入重新进入可用状态——必须被阻止。"""

        self.register("B-Q", "REF-Q")
        self.service.record_decision("qa", "B-Q", "quarantine", "封记异常")
        self.assertEqual(self.service.get_batch("B-Q")["status"], "quarantined")
        replay = self.register("B-Q", "REF-Q", key="key-B-Q")
        self.assertEqual(replay["batch_id"], "B-Q")
        self.assertEqual(self.service.get_batch("B-Q")["status"], "quarantined")
        events = [r[0] for r in self.connection.execute(
            "SELECT count(*) FROM audit_events WHERE entity_id='B-Q'"
        ).fetchall()]
        self.assertEqual(events[0], 2)  # 仅登记 + 隔离，没有第二次登记事件


class CorrectionTests(FuelServiceTestBase):
    def test_correction_appends_version_and_keeps_basis(self) -> None:
        self.register()
        self.clock.advance(hours=1)
        corrected = declaration()
        corrected["origin_doc_ref"] = "ORIGIN-2"
        history = self.service.correct_declaration("qa", "B-1", corrected)
        self.assertEqual([row["version"] for row in history], [1, 2])
        self.assertNotEqual(history[0]["basis_sha256"], history[1]["basis_sha256"])
        self.assertEqual(history[1]["supersedes_id"], history[0]["declaration_id"])
        self.assertEqual(self.service.get_batch("B-1")["declaration_version"], 2)

    def test_correction_cannot_change_source_identity(self) -> None:
        self.register()
        changed = declaration(reference="REF-2")
        with self.assertRaises(ValidationFailed):
            self.service.correct_declaration("qa", "B-1", changed)
        changed_type = declaration()
        changed_type["source_type"] = "imported"
        with self.assertRaises(ValidationFailed):
            self.service.correct_declaration("qa", "B-1", changed_type)

    def test_operator_cannot_correct(self) -> None:
        self.register()
        with self.assertRaises(Forbidden):
            self.service.correct_declaration("op", "B-1", declaration())

    def test_correction_does_not_change_current_balance_after_split(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.split_batch("op", "T-1", "B-1", [
            {"batch_id": "B-A", "quantity": "60"},
            {"batch_id": "B-B", "quantity": "40"},
        ])
        self.assertEqual(self.service.get_batch("B-1")["quantity"], "0")
        self.service.correct_declaration("qa", "B-1", declaration())
        self.assertEqual(self.service.get_batch("B-1")["quantity"], "0")


class InspectionDecisionTests(FuelServiceTestBase):
    def test_verdict_computed_from_limits(self) -> None:
        self.register()
        passed = self.inspect(results=PASS_RESULTS)
        self.assertEqual(passed["verdict"], "pass")
        failed = self.inspect(results=FAIL_RESULTS)
        self.assertEqual(failed["sequence_no"], 2)
        self.assertEqual(failed["verdict"], "fail")
        inconclusive = self.inspect(
            results={"purity": "0.99"},
            limits={"purity": {"min": "0.95"}, "impurity_ppm": {"max": "500"}},
        )
        self.assertEqual(inconclusive["verdict"], "inconclusive")

    def test_explicit_verdict_required_without_limits(self) -> None:
        self.register()
        with self.assertRaises(ValidationFailed):
            self.inspect(limits={})

    def test_release_requires_inspection(self) -> None:
        self.register()
        with self.assertRaises(InvalidState):
            self.release()

    def test_release_rejects_fail_verdict(self) -> None:
        self.register()
        failed = self.inspect(results=FAIL_RESULTS)
        with self.assertRaises(InvalidState):
            self.release(test_id=failed["test_id"])

    def test_four_eyes_principle(self) -> None:
        self.register()
        test = self.inspect(actor="qa")  # quality 也有检测权
        with self.assertRaises(Forbidden):
            self.release(test_id=test["test_id"], actor="qa")

    def test_release_then_quarantine_history_preserved(self) -> None:
        self.register()
        self.inspect_and_release()
        self.assertEqual(self.service.get_batch("B-1")["status"], "released")
        self.service.record_decision("qa", "B-1", "quarantine", "复检异常")
        self.assertEqual(self.service.get_batch("B-1")["status"], "quarantined")
        decisions = self.service.list_decisions("auditor", "B-1")
        self.assertEqual([row["decision"] for row in decisions], ["release", "quarantine"])

    def test_release_blocked_after_correction_until_reinspected(self) -> None:
        self.register()
        test = self.inspect_and_release()
        self.service.correct_declaration("qa", "B-1", declaration(quantity="100"))
        # 更正后原放行自动失效，批次回到隔离，旧检测不能再放行。
        self.assertEqual(self.service.get_batch("B-1")["status"], "quarantined")
        with self.assertRaises(InvalidState):
            self.release(test_id=test["test_id"])
        self.clock.advance(hours=1)
        self.inspect_and_release(analyst="analyst2")
        self.assertEqual(self.service.get_batch("B-1")["status"], "released")

    def test_inspection_records_basis_declaration_version(self) -> None:
        self.register()
        test_v1 = self.inspect()
        self.assertEqual(test_v1["basis_declaration_version"], 1)
        self.service.correct_declaration("qa", "B-1", declaration())
        test_v2 = self.inspect()
        self.assertEqual(test_v2["basis_declaration_version"], 2)


class TransformTests(FuelServiceTestBase):
    def test_split_only_for_released(self) -> None:
        self.register("B-1")
        with self.assertRaises(InvalidState):
            self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-A", "quantity": "50"}])
        self.service.record_decision("qa", "B-1", "quarantine", "隔离")
        with self.assertRaises(InvalidState):
            self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-A", "quantity": "50"}])

    def test_split_quantity_cannot_exceed_parent(self) -> None:
        self.register()
        self.inspect_and_release()
        with self.assertRaises(ValidationFailed):
            self.service.split_batch("op", "T-1", "B-1", [
                {"batch_id": "B-A", "quantity": "60"},
                {"batch_id": "B-B", "quantity": "50"},
            ])
        self.assertEqual(self.service.get_batch("B-1")["status"], "released")

    def test_split_partial_keeps_parent_released(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-A", "quantity": "30"}])
        parent = self.service.get_batch("B-1")
        self.assertEqual(parent["status"], "released")
        self.assertEqual(parent["quantity"], "70")
        child = self.service.get_batch("B-A")
        self.assertEqual(child["status"], "declared")
        self.assertEqual(child["quantity"], "30")

    def test_split_children_must_be_released_independently(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.split_batch("op", "T-1", "B-1", [
            {"batch_id": "B-A", "quantity": "60"},
            {"batch_id": "B-B", "quantity": "40"},
        ])
        self.assertEqual(self.service.get_batch("B-1")["status"], "exhausted")
        with self.assertRaises(InvalidState):
            self.service.dispatch_transfer(
                "custodian", "TR-1", "B-A", "甲", "乙", "库1", "库2"
            )
        self.inspect_and_release("B-A")
        transfer = self.service.dispatch_transfer(
            "custodian", "TR-1", "B-A", "甲", "乙", "库1", "库2"
        )
        self.assertEqual(transfer["status"], "dispatched")

    def test_merge_requires_conservation_and_compatible_parents(self) -> None:
        self.register("B-1", "REF-1", "60")
        self.register("B-2", "REF-2", "40")
        self.inspect_and_release("B-1")
        self.inspect_and_release("B-2")
        with self.assertRaises(ValidationFailed):
            self.service.merge_batches("op", "M-BAD", ["B-1", "B-2"],
                                       [{"batch_id": "B-M", "quantity": "99"}])
        other = declaration("B-3", "REF-3", "10")
        other["material_type"] = "UF6"
        self.service.register_declaration("op", other, "key-B-3")
        with self.assertRaises(InvalidState):
            self.service.merge_batches("op", "M-BAD2", ["B-1", "B-3"],
                                       [{"batch_id": "B-M", "quantity": "70"}])
        self.service.merge_batches("op", "M-1", ["B-1", "B-2"],
                                   [{"batch_id": "B-M", "quantity": "100"}])
        self.assertEqual(self.service.get_batch("B-1")["status"], "exhausted")
        self.assertEqual(self.service.get_batch("B-M")["status"], "declared")

    def test_merge_rejects_quarantined_input(self) -> None:
        self.register("B-1", "REF-1", "60")
        self.register("B-2", "REF-2", "40")
        self.inspect_and_release("B-1")
        self.service.record_decision("qa", "B-2", "quarantine", "污染")
        with self.assertRaises(InvalidState):
            self.service.merge_batches("op", "M-1", ["B-1", "B-2"],
                                       [{"batch_id": "B-M", "quantity": "100"}])

    def test_duplicate_transform_id_conflicts(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-A", "quantity": "10"}])
        self.inspect_and_release("B-A")
        self.inspect_and_release  # noqa: B018
        with self.assertRaises(Conflict):
            self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-B", "quantity": "10"}])

    def test_derived_components_blended_by_quantity(self) -> None:
        first = declaration("B-1", "REF-1", "75")
        first["components"] = [{"element": "U", "weight_fraction": "0.80"}]
        second = declaration("B-2", "REF-2", "25")
        second["components"] = [{"element": "U", "weight_fraction": "0.60"}]
        self.service.register_declaration("op", first, "key-B-1")
        self.service.register_declaration("op", second, "key-B-2")
        self.inspect_and_release("B-1")
        self.inspect_and_release("B-2")
        self.service.merge_batches("op", "M-1", ["B-1", "B-2"],
                                   [{"batch_id": "B-M", "quantity": "100"}])
        history = self.service.get_declaration_history("auditor", "B-M")
        from decimal import Decimal
        self.assertAlmostEqual(Decimal(history[0]["components"][0]["weight_fraction"]), Decimal("0.75"))


class TransferTests(FuelServiceTestBase):
    def _released(self, batch_id: str = "B-1") -> None:
        self.register(batch_id, f"REF-{batch_id}")
        self.inspect_and_release(batch_id)

    def test_dispatch_requires_current_release_basis(self) -> None:
        self.register()
        with self.assertRaises(InvalidState):
            self.service.dispatch_transfer(
                "custodian", "TR-1", "B-1", "甲", "乙", "库1", "库2"
            )
        self.inspect_and_release()
        self.service.record_decision("qa", "B-1", "quarantine", "暂停")
        with self.assertRaises(InvalidState):
            self.service.dispatch_transfer(
                "custodian", "TR-1", "B-1", "甲", "乙", "库1", "库2"
            )

    def test_full_handoff_restores_released(self) -> None:
        self._released()
        self.service.dispatch_transfer("custodian", "TR-1", "B-1", "甲", "乙", "库1", "库2")
        self.assertEqual(self.service.get_batch("B-1")["status"], "in_transit")
        with self.assertRaises(InvalidState):
            self.service.record_decision("qa", "B-1", "quarantine", "在途不能决定")
        received = self.service.receive_transfer("custodian2", "TR-1", "完好")
        self.assertEqual(received["status"], "received")
        self.assertEqual(self.service.get_batch("B-1")["status"], "released")

    def test_double_receive_blocked(self) -> None:
        self._released()
        self.service.dispatch_transfer("custodian", "TR-1", "B-1", "甲", "乙", "库1", "库2")
        self.service.receive_transfer("custodian2", "TR-1")
        with self.assertRaises(InvalidState):
            self.service.receive_transfer("custodian2", "TR-1")

    def test_receive_quarantines_when_basis_invalidated_in_transit(self) -> None:
        self._released()
        self.service.dispatch_transfer("custodian", "TR-1", "B-1", "甲", "乙", "库1", "库2")
        # 在途期间来源信息被更正：放行依据失效。
        self.clock.advance(hours=1)
        self.service.correct_declaration("qa", "B-1", declaration(reference="REF-B-1"))
        self.service.receive_transfer("custodian2", "TR-1")
        self.assertEqual(self.service.get_batch("B-1")["status"], "quarantined")
        events = [r[0] for r in self.connection.execute(
            "SELECT payload_json FROM audit_events WHERE event_type='transfer.received'"
        ).fetchall()]
        self.assertIn('"release_basis_invalidated":true', events[0])


class DisposalTests(FuelServiceTestBase):
    def test_disposal_is_terminal(self) -> None:
        self.register()
        self.service.record_decision("qa", "B-1", "quarantine", "污染超标")
        self.service.dispose_batch("qa", "B-1", "固化处置", "AUTH-1", "无法去污", witness="auditor")
        self.assertEqual(self.service.get_batch("B-1")["status"], "disposed")
        with self.assertRaises(InvalidState):
            self.inspect()
        with self.assertRaises(InvalidState):
            self.service.record_decision("qa", "B-1", "release", "尝试复活")
        with self.assertRaises(InvalidState):
            self.service.correct_declaration("qa", "B-1", declaration())
        with self.assertRaises(InvalidState):
            self.service.dispose_batch("qa", "B-1", "再次处置", "AUTH-2", "x", witness="auditor")

    def test_disposal_requires_distinct_witness(self) -> None:
        self.register()
        with self.assertRaises(Forbidden):
            self.service.dispose_batch("qa", "B-1", "固化", "AUTH-1", "r", witness="qa")

    def test_disposal_blocked_in_transit(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.dispatch_transfer("custodian", "TR-1", "B-1", "甲", "乙", "库1", "库2")
        with self.assertRaises(InvalidState):
            self.service.dispose_batch("qa", "B-1", "固化", "AUTH-1", "r", witness="auditor")


class TraceImpactTests(FuelServiceTestBase):
    def _lineage(self) -> None:
        self.register("B-1", "REF-1", "100")
        self.inspect_and_release("B-1")
        self.service.split_batch("op", "T-1", "B-1", [
            {"batch_id": "B-A", "quantity": "60"},
            {"batch_id": "B-B", "quantity": "40"},
        ])
        self.inspect_and_release("B-A")
        self.inspect_and_release("B-B")

    def test_trace_shows_lineage_and_every_event(self) -> None:
        self._lineage()
        trace = self.service.trace_batch("auditor", "B-1")
        self.assertEqual([child["batch_id"] for child in trace["children"]], ["B-A", "B-B"])
        self.assertEqual(trace["batch"]["status"], "exhausted")
        types = {event["event_type"] for event in trace["events"]}
        self.assertIn("declaration.registered", types)
        self.assertIn("batch.split", types)
        child_trace = self.service.trace_batch("auditor", "B-A")
        self.assertEqual(child_trace["parents"][0]["parent_batch_id"], "B-1")

    def test_impact_before_correction_is_empty(self) -> None:
        self._lineage()
        impact = self.service.impact_analysis("auditor", "B-1")
        self.assertFalse(impact["corrected"])
        self.assertEqual(impact["affected_count"], 0)

    def test_impact_marks_affected_revalidated_and_irreversible(self) -> None:
        self._lineage()
        self.clock.advance(hours=2)
        self.service.correct_declaration("qa", "B-1", declaration())
        impact = self.service.impact_analysis("auditor", "B-1")
        by_id = {item["batch_id"]: item for item in impact["descendants"]}
        self.assertTrue(by_id["B-A"]["affected_by_correction"])
        self.assertTrue(by_id["B-B"]["affected_by_correction"])
        self.assertFalse(by_id["B-A"]["revalidated_after_correction"])

        # A 更正后复检再放行 → 已重新验证。
        self.clock.advance(hours=1)
        self.inspect_and_release("B-A", analyst="analyst2")
        # B 隔离并不可逆处置。
        self.service.record_decision("qa", "B-B", "quarantine", "暂停")
        self.service.dispose_batch("qa", "B-B", "固化", "AUTH-9", "杂质高", witness="auditor")
        impact = self.service.impact_analysis("auditor", "B-1")
        by_id = {item["batch_id"]: item for item in impact["descendants"]}
        self.assertTrue(by_id["B-A"]["revalidated_after_correction"])
        self.assertFalse(by_id["B-B"]["revalidated_after_correction"])
        self.assertTrue(by_id["B-B"]["irreversible"])
        self.assertEqual(impact["irreversible_count"], 1)

    def test_impact_propagates_through_multiple_levels(self) -> None:
        self._lineage()
        self.service.split_batch("op", "T-2", "B-A", [{"batch_id": "B-A1", "quantity": "60"}])
        self.inspect_and_release("B-A1")
        self.clock.advance(hours=2)
        self.service.correct_declaration("qa", "B-1", declaration())
        impact = self.service.impact_analysis("auditor", "B-1")
        by_id = {item["batch_id"]: item for item in impact["descendants"]}
        self.assertTrue(by_id["B-A1"]["affected_by_correction"])
        self.assertEqual(by_id["B-A1"]["depth"], 2)

    def test_ancestor_correction_cascades_through_merge_and_split(self) -> None:
        first = declaration("P1", "R1", "60")
        second = declaration("P2", "R2", "40")
        self.service.register_declaration("op", first, "key-P1")
        self.service.register_declaration("op", second, "key-P2")
        self.inspect_and_release("P1")
        self.inspect_and_release("P2")
        self.service.merge_batches("op", "MG", ["P1", "P2"],
                                   [{"batch_id": "C", "quantity": "100"}])
        self.inspect_and_release("C", analyst="analyst2")
        self.service.split_batch("op", "SP", "C", [{"batch_id": "G", "quantity": "100"}])
        self.inspect_and_release("G", analyst="analyst2")
        self.clock.advance(hours=2)
        self.service.correct_declaration("qa", "P1", declaration("P1", "R1", "60"))
        # 孙批次的放行依据必须随祖父更正自动失效。
        self.assertEqual(self.service.get_batch("G")["status"], "quarantined")
        impact = self.service.impact_analysis("auditor", "P1")
        by_id = {item["batch_id"]: item for item in impact["descendants"]}
        self.assertTrue(by_id["C"]["affected_by_correction"])
        self.assertTrue(by_id["G"]["affected_by_correction"])
        self.assertEqual(by_id["G"]["depth"], 2)

    def test_split_rejects_existing_output_batch(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-A", "quantity": "10"}])
        with self.assertRaises(Conflict):
            self.service.split_batch("op", "T-2", "B-1", [{"batch_id": "B-A", "quantity": "10"}])

    def test_stale_inspection_cannot_release_after_ancestor_correction(self) -> None:
        self.register()
        self.inspect_and_release()
        self.service.split_batch("op", "T-1", "B-1", [{"batch_id": "B-A", "quantity": "100"}])
        self.inspect_and_release("B-A", analyst="analyst2")
        stale = self.service.list_inspections("auditor", "B-A")[0]
        self.clock.advance(hours=1)
        self.service.correct_declaration("qa", "B-1", declaration())
        with self.assertRaises(InvalidState):
            self.release("B-A", test_id=stale["test_id"])

    def test_audit_chain_detects_tampering(self) -> None:
        self._lineage()
        self.assertTrue(self.service.verify_chain("auditor")["valid"])
        self.connection.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=1", ('{"tampered":true}',)
        )
        result = self.service.verify_chain("auditor")
        self.assertFalse(result["valid"])
        self.assertEqual(result["broken_at"], 1)


if __name__ == "__main__":
    unittest.main()
