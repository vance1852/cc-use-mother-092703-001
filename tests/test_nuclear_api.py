from __future__ import annotations

import json
import sqlite3
import unittest

from nuclear_chain.api import JsonApplication
from nuclear_chain.service import BatchService


COMPOSITION = [
    {"element": "U", "isotope": "U-235", "fraction": "0.04"},
    {"element": "U", "isotope": "U-238", "fraction": "0.96"},
]


def batch_payload(batch_id: str = "B-1", quantity: str = "100") -> dict:
    return {
        "batch_id": batch_id,
        "material_type": "UF6",
        "quantity": quantity,
        "unit": "kgU",
        "source": {"supplier": "西北铀浓缩厂", "origin": "Y-2", "document_ref": "SRC-1"},
        "composition": COMPOSITION,
        "basis_doc": "质保书 QA-1",
    }


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        service = BatchService(self.connection)
        for user_id, role in (
            ("reg", "registrar"),
            ("lab", "analyst"),
            ("qa", "quality"),
            ("ship", "logistics"),
            ("rec", "recovery"),
            ("audit", "auditor"),
        ):
            service.create_user(user_id, user_id, role)
        self.app = JsonApplication(service)

    def tearDown(self) -> None:
        self.connection.close()

    def _call(self, method: str, path: str, actor: str | None = "reg", payload: dict | None = None):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"Content-Type": "application/json"}
        if actor is not None:
            headers["X-Actor-Id"] = actor
        return self.app.handle(method, path, headers, body)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["service"], "nuclear-chain")

    def test_requires_actor_header(self) -> None:
        response = self._call("POST", "/batches", actor=None, payload=batch_payload())
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_register_and_get(self) -> None:
        response = self._call("POST", "/batches", payload=batch_payload())
        self.assertEqual(response.status, 201)
        fetched = self._call("GET", "/batches/B-1")
        self.assertEqual(fetched.status, 200)
        self.assertEqual(fetched.body["status"], "registered")

    def test_forbidden_role(self) -> None:
        response = self._call("POST", "/batches", actor="lab", payload=batch_payload())
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")

    def test_quarantine_sticky_across_manifest_workflow(self) -> None:
        self._call("POST", "/batches", payload=batch_payload("B-1"))
        test = self._call("POST", "/batches/B-1/tests", actor="lab", payload={
            "idempotency_key": "t-1", "method": "ICP-MS", "instrument": "ICP-1",
            "results": {"u235": "0.03"}, "verdict": "nonconforming", "basis_doc": "TR-1",
        })
        self.assertEqual(test.status, 201)
        decision = self._call("POST", "/batches/B-1/decisions", actor="qa", payload={
            "decision": "quarantine", "reason": "丰度不足",
        })
        self.assertEqual(decision.body["status"], "quarantined")
        manifest = self._call("POST", "/manifests/import", payload={
            "idempotency_key": "mf-1", "source_ref": "EXT-1",
            "entries": [batch_payload("B-1")],
        })
        self.assertEqual(manifest.status, 201)
        self.assertEqual(manifest.body["preserved_quarantine"], ["B-1"])
        fetched = self._call("GET", "/batches/B-1")
        self.assertEqual(fetched.body["status"], "quarantined")

    def test_trace_and_lineage_routes(self) -> None:
        self._call("POST", "/batches", payload=batch_payload("B-1"))
        trace = self._call("GET", "/batches/B-1/trace", actor="audit")
        self.assertEqual(trace.status, 200)
        self.assertEqual(trace.body["batch"]["batch_id"], "B-1")
        lineage = self._call("GET", "/batches/B-1/lineage", actor="audit")
        self.assertEqual(lineage.body["descendant_count"], 0)

    def test_disposition_route_blocks_revival(self) -> None:
        self._call("POST", "/batches", payload=batch_payload("B-1"))
        disposed = self._call("POST", "/dispositions", actor="rec", payload={
            "batch_id": "B-1", "kind": "discard", "quantity": "100",
            "method": "弃置", "facility": "F", "document_ref": "D",
            "idempotency_key": "d-1",
        })
        self.assertEqual(disposed.status, 201)
        retry = self._call("POST", "/batches/B-1/handoffs", actor="ship", payload={
            "handoff_type": "ship", "from_party": "a", "to_party": "b",
            "document_ref": "W", "observed_quantity": "1",
            "idempotency_key": "h-1",
        })
        self.assertEqual(retry.status, 409)
        self.assertEqual(retry.body["error"]["code"], "invalid_state")

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nope", {"X-Actor-Id": "audit"})
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
