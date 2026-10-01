from __future__ import annotations

import json
import sqlite3
import unittest

from fuel_cycle.api import JsonApplication
from fuel_cycle.service import FuelCycleService


def body(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode()


DECLARATION = {
    "batch_id": "B-1",
    "source_type": "mining",
    "source_reference": "REF-1",
    "supplier": "南方铀业",
    "origin_doc_ref": "ORIGIN-1",
    "material_type": "U3O8",
    "quantity": "100",
    "unit": "kg",
    "components": [{"element": "U", "weight_fraction": "0.848"}],
    "received_at": "2026-09-28T02:00:00Z",
}


class FuelApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(FuelCycleService(self.connection))
        for user_id, role in (
            ("op", "operator"),
            ("analyst", "analyst"),
            ("qa", "quality"),
            ("custodian", "custodian"),
            ("auditor", "auditor"),
        ):
            self.app.handle("POST", "/users", body=body(
                {"user_id": user_id, "display_name": user_id, "role": role}
            ))

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["service"], "fuel-cycle-traceability")

    def test_requires_actor_header(self) -> None:
        response = self.app.handle("GET", "/batches/B-1/trace")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_bad_json(self) -> None:
        response = self.app.handle("POST", "/users", body=b"{")
        self.assertEqual(response.status, 422)

    def test_not_found_route_and_entity(self) -> None:
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)
        response = self.app.handle(
            "GET", "/batches/MISSING/trace", headers={"X-Actor-Id": "auditor"}
        )
        self.assertEqual(response.status, 404)

    def test_full_flow_over_http(self) -> None:
        response = self.app.handle(
            "POST", "/declarations",
            headers={"X-Actor-Id": "op", "Idempotency-Key": "imp-1"},
            body=body(DECLARATION),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["status"], "declared")

        response = self.app.handle(
            "POST", "/batches/B-1/inspections",
            headers={"X-Actor-Id": "analyst"},
            body=body({
                "test_type": "成分复检", "lab": "中心实验室", "method": "M-1",
                "results": {"purity": "0.996"},
                "limits": {"purity": {"min": "0.95"}},
            }),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["verdict"], "pass")
        test_id = response.body["test_id"]

        response = self.app.handle(
            "POST", "/batches/B-1/decisions",
            headers={"X-Actor-Id": "qa"},
            body=body({"decision": "release", "reason": "合格", "basis_test_id": test_id}),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["status"], "released")

        response = self.app.handle(
            "POST", "/batches/B-1/split",
            headers={"X-Actor-Id": "op"},
            body=body({"transform_id": "T-1", "parts": [{"batch_id": "B-A", "quantity": "40"}]}),
        )
        self.assertEqual(response.status, 201)
        self.assertEqual(response.body["outputs"][0]["batch_id"], "B-A")

        response = self.app.handle(
            "GET", "/batches/B-1/impact", headers={"X-Actor-Id": "auditor"}
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["descendants"][0]["batch_id"], "B-A")

    def test_quarantine_cannot_be_revived_by_replay_over_http(self) -> None:
        headers = {"X-Actor-Id": "op", "Idempotency-Key": "imp-q"}
        self.app.handle("POST", "/declarations", headers=headers, body=body(DECLARATION))
        self.app.handle(
            "POST", "/batches/B-1/decisions",
            headers={"X-Actor-Id": "qa"},
            body=body({"decision": "quarantine", "reason": "封记异常"}),
        )
        replay = self.app.handle("POST", "/declarations", headers=headers, body=body(DECLARATION))
        self.assertEqual(replay.status, 201)
        current = self.app.handle("GET", "/batches/B-1", headers={"X-Actor-Id": "auditor"})
        self.assertEqual(current.body["status"], "quarantined")

    def test_dispatch_and_receive_routes(self) -> None:
        self.app.handle(
            "POST", "/declarations",
            headers={"X-Actor-Id": "op", "Idempotency-Key": "imp-1"},
            body=body(DECLARATION),
        )
        self.app.handle(
            "POST", "/batches/B-1/inspections",
            headers={"X-Actor-Id": "analyst"},
            body=body({"test_type": "t", "lab": "l", "method": "m",
                       "results": {"purity": "0.99"}, "limits": {"purity": {"min": "0.95"}}}),
        )
        self.app.handle(
            "POST", "/batches/B-1/decisions",
            headers={"X-Actor-Id": "qa"},
            body=body({"decision": "release", "reason": "ok"}),
        )
        dispatched = self.app.handle(
            "POST", "/transfers",
            headers={"X-Actor-Id": "custodian"},
            body=body({"transfer_id": "TR-1", "batch_id": "B-1", "from_party": "甲",
                       "to_party": "乙", "from_location": "库1", "to_location": "库2"}),
        )
        self.assertEqual(dispatched.status, 201)
        received = self.app.handle(
            "POST", "/transfers/TR-1/receive",
            headers={"X-Actor-Id": "custodian"},
            body=body({"note": "完好"}),
        )
        self.assertEqual(received.status, 200)
        self.assertEqual(received.body["status"], "received")

    def test_audit_verify_route(self) -> None:
        self.app.handle(
            "POST", "/declarations",
            headers={"X-Actor-Id": "op", "Idempotency-Key": "imp-1"},
            body=body(DECLARATION),
        )
        response = self.app.handle(
            "POST", "/audit/verify", body=b"", headers={"X-Actor-Id": "auditor"}
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["valid"])
        self.assertEqual(response.body["event_count"], 1)


if __name__ == "__main__":
    unittest.main()
