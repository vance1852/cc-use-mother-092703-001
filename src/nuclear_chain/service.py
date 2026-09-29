"""核燃料循环批次监管的领域用例。

覆盖来源与成分声明登记、分批/合批、检测登记、有权人员的放行/隔离
决定、运输交接、不可逆处置与清单导入。所有变更只追加原始依据，并
写入哈希链审计事件。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, digest, quantity_text
from .storage import initialize, transaction


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "registrar": {"batch.register", "declaration.correct", "manifest.import", "trace.read"},
    "analyst": {"test.record", "trace.read"},
    "quality": {"decision.write", "trace.read"},
    "logistics": {"custody.write", "trace.read"},
    "recovery": {"disposition.write", "trace.read"},
    "auditor": {"audit.read", "trace.read"},
}
ROLE_PERMISSIONS["admin"] = {
    permission for grants in ROLE_PERMISSIONS.values() for permission in grants
}

_DISPOSED = "disposed"
_QUARANTINED = "quarantined"
_DISPOSITION_KINDS = ("recovery", "discard", "final_storage")
_VERDICTS = ("conforming", "nonconforming", "inconclusive")
_DECISIONS = ("release", "quarantine", "reject")
_HANDOFF_TYPES = ("ship", "receive", "return")


class BatchService:
    """在单个 SQLite 连接上提供全部批次监管操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # --------------------------------------------------------------- 基础辅助

    def _now(self) -> str:
        return utc_text(self.clock.now())

    @contextmanager
    def _unit(self) -> Iterator[None]:
        try:
            with transaction(self.connection, immediate=True):
                yield
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"数据一致性冲突: {exc}") from exc

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id,display_name,role,active FROM chain_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        with self._unit():
            self.connection.execute(
                "INSERT INTO chain_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                (user_id.strip(), display_name.strip(), role, self._now()),
            )
        return {"user_id": user_id.strip(), "role": role}

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> str:
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        created_at = self._now()
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": created_at,
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                created_at,
            ),
        )
        return event_hash

    def _replay(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return {**json.loads(row["response_json"]), "replayed": True}

    def _store_replay(
        self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------- 声明校验

    @staticmethod
    def _text(raw: Mapping[str, Any], field: str) -> str:
        value = raw.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 不能为空")
        return value.strip()

    def _source(self, raw: Mapping[str, Any]) -> dict[str, str]:
        source = raw.get("source")
        if not isinstance(source, dict):
            raise ValidationFailed("source 必须是来源声明对象")
        supplier = source.get("supplier")
        if not isinstance(supplier, str) or not supplier.strip():
            raise ValidationFailed("source.supplier 不能为空")
        return {
            "supplier": supplier.strip(),
            "origin": str(source.get("origin") or "").strip(),
            "document_ref": str(source.get("document_ref") or "").strip(),
        }

    def _composition(self, raw: Mapping[str, Any]) -> list[dict[str, str]]:
        items = raw.get("composition")
        if not isinstance(items, list) or not items:
            raise ValidationFailed("composition 至少要声明一种核素成分")
        normalized: list[dict[str, str]] = []
        total = Decimal("0")
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                raise ValidationFailed("composition 条目必须是对象")
            element = item.get("element")
            if not isinstance(element, str) or not element.strip():
                raise ValidationFailed("composition.element 不能为空")
            isotope = str(item.get("isotope") or "").strip()
            label = f"{element.strip()}:{isotope}"
            if label in seen:
                raise ValidationFailed(f"成分重复声明: {label}")
            seen.add(label)
            try:
                fraction = Decimal(str(item["fraction"]))
            except Exception as exc:  # noqa: BLE001
                raise ValidationFailed(f"{label} 的 fraction 必须是数值") from exc
            if not fraction.is_finite() or fraction <= 0 or fraction > 1:
                raise ValidationFailed(f"{label} 的 fraction 必须在 0 到 1 之间")
            total += fraction
            normalized.append(
                {"element": element.strip(), "isotope": isotope, "fraction": format(fraction, "f")}
            )
        if abs(total - Decimal("1")) > Decimal("0.000001"):
            raise ValidationFailed(f"成分份额之和必须为 1，当前为 {total}")
        return normalized

    def _declaration_fields(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "source": self._source(raw),
            "composition": self._composition(raw),
            "basis_doc": self._text(raw, "basis_doc"),
        }

    def _insert_declaration(
        self,
        batch_id: str,
        revision_no: int,
        supersedes_id: int | None,
        fields: Mapping[str, Any],
        actor_id: str,
        now: str,
    ) -> int:
        content = {"source": fields["source"], "composition": fields["composition"]}
        cursor = self.connection.execute(
            "INSERT INTO declarations(batch_id,revision_no,supersedes_id,source_json,composition_json,"
            "basis_doc,content_sha256,declared_by,declared_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                batch_id,
                revision_no,
                supersedes_id,
                canonical_json(fields["source"]),
                canonical_json(fields["composition"]),
                fields["basis_doc"],
                digest(content),
                actor_id,
                now,
            ),
        )
        return int(cursor.lastrowid)

    # --------------------------------------------------------------- 批次读取

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return dict(self._batch_row(batch_id))

    def _batch_row(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"批次不存在: {batch_id}")
        return row

    def _require_active(self, row: sqlite3.Row) -> None:
        if row["status"] == _DISPOSED:
            raise InvalidState(f"批次 {row['batch_id']} 已进入不可逆处置，不能再变更")

    def _insert_batch(
        self,
        batch_id: str,
        material_type: str,
        quantity: str,
        unit: str,
        status: str,
        actor_id: str,
        now: str,
    ) -> None:
        self.connection.execute(
            "INSERT INTO batches(batch_id,material_type,quantity_text,unit,status,"
            "current_declaration_id,revision,remaining_quantity_text,created_by,created_at,updated_at) "
            "VALUES(?,?,?,?,?,NULL,1,?,?,?,?)",
            (batch_id, material_type, quantity, unit, status, quantity, actor_id, now, now),
        )

    # --------------------------------------------------------------- 批次登记

    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.register")
        batch_id = self._text(raw, "batch_id")
        material_type = self._text(raw, "material_type")
        unit = self._text(raw, "unit")
        quantity = quantity_text(raw.get("quantity"), "quantity")
        fields = self._declaration_fields(raw)
        idempotency_key = raw.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = self._text(raw, "idempotency_key")
        request_digest = digest(raw)
        with self._unit():
            if idempotency_key:
                replayed = self._replay("register", idempotency_key, request_digest)
                if replayed is not None:
                    return replayed
            if self.connection.execute(
                "SELECT 1 FROM batches WHERE batch_id=?", (batch_id,)
            ).fetchone():
                raise Conflict(f"批次编号已存在: {batch_id}")
            now = self._now()
            self._insert_batch(batch_id, material_type, quantity, unit, "registered", actor_id, now)
            declaration_id = self._insert_declaration(batch_id, 1, None, fields, actor_id, now)
            self.connection.execute(
                "UPDATE batches SET current_declaration_id=? WHERE batch_id=?",
                (declaration_id, batch_id),
            )
            response = {
                "batch_id": batch_id,
                "status": "registered",
                "declaration_id": declaration_id,
            }
            if idempotency_key:
                self._store_replay("register", idempotency_key, request_digest, response)
            self._audit("batch", batch_id, "batch.registered", actor_id, {
                "material_type": material_type,
                "quantity": quantity,
                "unit": unit,
                "source": fields["source"],
                "basis_doc": fields["basis_doc"],
                "declaration_id": declaration_id,
            })
        return response

    # ------------------------------------------------------------- 来源更正

    def correct_declaration(
        self,
        actor_id: str,
        batch_id: str,
        expected_revision: int,
        reason: str,
        raw: Mapping[str, Any],
    ) -> dict[str, Any]:
        """登记来源/成分更正；原始声明保留为历史版本，不覆盖。"""
        self._require(actor_id, "declaration.correct")
        if not reason.strip():
            raise ValidationFailed("更正原因不能为空")
        fields = self._declaration_fields(raw)
        with self._unit():
            batch = self._batch_row(batch_id)
            self._require_active(batch)
            if batch["revision"] != expected_revision:
                raise InvalidState("批次不是当前版本，刷新后重试")
            now = self._now()
            declaration_id = self._insert_declaration(
                batch_id, expected_revision + 1, batch["current_declaration_id"], fields, actor_id, now
            )
            self.connection.execute(
                "UPDATE batches SET current_declaration_id=?,revision=revision+1,updated_at=? "
                "WHERE batch_id=?",
                (declaration_id, now, batch_id),
            )
            descendants = self._descendant_rows(batch_id)
            self._audit("batch", batch_id, "declaration.corrected", actor_id, {
                "supersedes_id": batch["current_declaration_id"],
                "new_declaration_id": declaration_id,
                "revision": expected_revision + 1,
                "reason": reason.strip(),
                "impacted_descendants": [item["batch_id"] for item in descendants],
                "disposed_descendants": [
                    item["batch_id"] for item in descendants if item["status"] == _DISPOSED
                ],
            })
        return {
            "batch_id": batch_id,
            "declaration_id": declaration_id,
            "revision": expected_revision + 1,
        }

    # ----------------------------------------------------------------- 检测

    def record_test(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        method: str,
        instrument: str,
        results: Mapping[str, Any],
        verdict: str,
        basis_doc: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "test.record")
        if not method.strip() or not instrument.strip() or not basis_doc.strip():
            raise ValidationFailed("method、instrument 和 basis_doc 不能为空")
        if verdict not in _VERDICTS:
            raise ValidationFailed(f"verdict 必须是 {_VERDICTS} 之一")
        if not isinstance(results, dict) or not results:
            raise ValidationFailed("results 必须是非空检测结果对象")
        request_digest = digest({
            "batch_id": batch_id,
            "method": method,
            "instrument": instrument,
            "results": results,
            "verdict": verdict,
            "basis_doc": basis_doc,
        })
        with self._unit():
            replayed = self._replay(f"test:{batch_id}", idempotency_key, request_digest)
            if replayed is not None:
                return replayed
            batch = self._batch_row(batch_id)
            self._require_active(batch)
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO tests(batch_id,method,instrument,results_json,verdict,basis_doc,"
                "content_sha256,idempotency_key,recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    method.strip(),
                    instrument.strip(),
                    canonical_json(results),
                    verdict,
                    basis_doc.strip(),
                    request_digest,
                    idempotency_key,
                    actor_id,
                    now,
                ),
            )
            test_id = int(cursor.lastrowid)
            if batch["status"] == "registered":
                self.connection.execute(
                    "UPDATE batches SET status='in_test',updated_at=? WHERE batch_id=?",
                    (now, batch_id),
                )
            response = {"test_id": test_id, "batch_id": batch_id, "verdict": verdict}
            self._store_replay(f"test:{batch_id}", idempotency_key, request_digest, response)
            self._audit("batch", batch_id, "test.recorded", actor_id, {
                "test_id": test_id,
                "verdict": verdict,
                "method": method.strip(),
                "basis_doc": basis_doc.strip(),
            })
        return response

    # ------------------------------------------------------------- 放行决定

    def decide(
        self,
        actor_id: str,
        batch_id: str,
        decision: str,
        reason: str,
        test_id: int | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "decision.write")
        if decision not in _DECISIONS:
            raise ValidationFailed(f"decision 必须是 {_DECISIONS} 之一")
        if not reason.strip():
            raise ValidationFailed("放行决定必须填写理由")
        with self._unit():
            batch = self._batch_row(batch_id)
            self._require_active(batch)
            target_status: str
            if decision == "release":
                conforming = self._conforming_test(batch_id, test_id)
                if conforming is None:
                    raise InvalidState("放行必须以合格检测结论为依据")
                if batch["status"] == _QUARANTINED and not self._retested_after_quarantine(batch_id):
                    raise InvalidState("隔离批次须在隔离之后完成复检并取得合格结论，才能由质量人员放行")
                target_status = "released"
            else:
                target_status = _QUARANTINED  # quarantine（隔离待查）与 reject（拒收）均冻结为隔离
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO decisions(batch_id,decision,reason,test_id,decided_by,decided_at) "
                "VALUES(?,?,?,?,?,?)",
                (batch_id, decision, reason.strip(), test_id, actor_id, now),
            )
            decision_id = int(cursor.lastrowid)
            self.connection.execute(
                "UPDATE batches SET status=?,updated_at=? WHERE batch_id=?",
                (target_status, now, batch_id),
            )
            self._audit("batch", batch_id, "decision.recorded", actor_id, {
                "decision_id": decision_id,
                "decision": decision,
                "status": target_status,
                "reason": reason.strip(),
                "test_id": test_id,
            })
        return {
            "batch_id": batch_id,
            "decision_id": decision_id,
            "decision": decision,
            "status": target_status,
        }

    def _conforming_test(self, batch_id: str, test_id: int | None) -> sqlite3.Row | None:
        if test_id is not None:
            return self.connection.execute(
                "SELECT * FROM tests WHERE test_id=? AND batch_id=? AND verdict='conforming'",
                (test_id, batch_id),
            ).fetchone()
        return self.connection.execute(
            "SELECT * FROM tests WHERE batch_id=? AND verdict='conforming' "
            "ORDER BY test_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()

    def _retested_after_quarantine(self, batch_id: str) -> bool:
        last_quarantine = self.connection.execute(
            "SELECT decided_at FROM decisions WHERE batch_id=? AND decision IN ('quarantine','reject') "
            "ORDER BY decision_id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if last_quarantine is None:
            return True
        return self.connection.execute(
            "SELECT 1 FROM tests WHERE batch_id=? AND verdict='conforming' AND recorded_at>? LIMIT 1",
            (batch_id, last_quarantine["decided_at"]),
        ).fetchone() is not None

    # ------------------------------------------------------------- 运输交接

    def handoff(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "custody.write")
        batch_id = self._text(raw, "batch_id")
        handoff_type = self._text(raw, "handoff_type")
        if handoff_type not in _HANDOFF_TYPES:
            raise ValidationFailed(f"handoff_type 必须是 {_HANDOFF_TYPES} 之一")
        from_party = self._text(raw, "from_party")
        to_party = self._text(raw, "to_party")
        document_ref = self._text(raw, "document_ref")
        observed = quantity_text(raw.get("observed_quantity"), "observed_quantity")
        idempotency_key = self._text(raw, "idempotency_key")
        request_digest = digest(raw)
        with self._unit():
            replayed = self._replay(f"custody:{batch_id}", idempotency_key, request_digest)
            if replayed is not None:
                return replayed
            batch = self._batch_row(batch_id)
            self._require_active(batch)
            if Decimal(observed) > Decimal(batch["remaining_quantity_text"]):
                raise ValidationFailed("交接数量不能超过批次结余数量")
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO custody_events(batch_id,handoff_type,from_party,to_party,document_ref,"
                "observed_quantity_text,note,idempotency_key,handled_by,occurred_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    handoff_type,
                    from_party,
                    to_party,
                    document_ref,
                    observed,
                    str(raw.get("note") or "").strip(),
                    idempotency_key,
                    actor_id,
                    now,
                ),
            )
            custody_id = int(cursor.lastrowid)
            response = {
                "custody_id": custody_id,
                "batch_id": batch_id,
                "handoff_type": handoff_type,
            }
            self._store_replay(f"custody:{batch_id}", idempotency_key, request_digest, response)
            self._audit("batch", batch_id, "custody.handoff", actor_id, {
                "custody_id": custody_id,
                "handoff_type": handoff_type,
                "from_party": from_party,
                "to_party": to_party,
                "document_ref": document_ref,
                "observed_quantity": observed,
            })
        return response

    # ------------------------------------------------------------- 分批合批

    def _inherit_quarantine_decision(
        self, child_id: str, reason: str, actor_id: str, now: str
    ) -> None:
        """子批次天生带隔离时，补一条隔离决定作为复检门槛与追溯依据。"""
        self.connection.execute(
            "INSERT INTO decisions(batch_id,decision,reason,test_id,decided_by,decided_at) "
            "VALUES(?, 'quarantine', ?, NULL, ?, ?)",
            (child_id, reason, actor_id, now),
        )

    def split_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.register")
        parent_id = self._text(raw, "parent_id")
        split_id = self._text(raw, "split_id")
        idempotency_key = self._text(raw, "idempotency_key")
        parts_raw = raw.get("parts")
        if not isinstance(parts_raw, list) or len(parts_raw) < 2:
            raise ValidationFailed("分批至少要声明两个子批次")
        parts: list[dict[str, str]] = []
        total = Decimal("0")
        for item in parts_raw:
            if not isinstance(item, dict):
                raise ValidationFailed("parts 条目必须是对象")
            amount = quantity_text(item.get("quantity"), "quantity")
            total += Decimal(amount)
            parts.append({"batch_id": self._text(item, "batch_id"), "quantity": amount})
        request_digest = digest(raw)
        with self._unit():
            replayed = self._replay(f"split:{parent_id}", idempotency_key, request_digest)
            if replayed is not None:
                return replayed
            parent = self._batch_row(parent_id)
            self._require_active(parent)
            if total > Decimal(parent["remaining_quantity_text"]):
                raise ValidationFailed("分批数量之和超过批次结余数量")
            child_ids = [part["batch_id"] for part in parts]
            if len(set(child_ids)) != len(child_ids):
                raise ValidationFailed("同一次分批内子批次编号重复")
            now = self._now()
            inherited_quarantine = parent["status"] == _QUARANTINED
            child_status = _QUARANTINED if inherited_quarantine else parent["status"]
            declaration = self.connection.execute(
                "SELECT * FROM declarations WHERE declaration_id=?",
                (parent["current_declaration_id"],),
            ).fetchone()
            for part in parts:
                if self.connection.execute(
                    "SELECT 1 FROM batches WHERE batch_id=?", (part["batch_id"],)
                ).fetchone():
                    raise Conflict(f"子批次编号已存在: {part['batch_id']}")
                self._insert_batch(
                    part["batch_id"], parent["material_type"], part["quantity"],
                    parent["unit"], child_status, actor_id, now,
                )
                child_declaration = self._copy_declaration(
                    declaration, part["batch_id"], actor_id, now
                )
                self.connection.execute(
                    "UPDATE batches SET current_declaration_id=? WHERE batch_id=?",
                    (child_declaration, part["batch_id"]),
                )
                self.connection.execute(
                    "INSERT INTO lineage_edges(parent_id,child_id,operation_kind,operation_id,"
                    "quantity_text,declaration_id,declaration_revision,created_at) "
                    "VALUES(?,?,'split',?,?,?,?,?)",
                    (
                        parent_id,
                        part["batch_id"],
                        split_id,
                        part["quantity"],
                        parent["current_declaration_id"],
                        declaration["revision_no"],
                        now,
                    ),
                )
                if inherited_quarantine:
                    self._inherit_quarantine_decision(
                        part["batch_id"],
                        f"隔离状态继承自已隔离父批次 {parent_id}（分批 {split_id}）",
                        actor_id,
                        now,
                    )
                self._audit("batch", part["batch_id"], "batch.created_from_split", actor_id, {
                    "split_id": split_id,
                    "parent_id": parent_id,
                    "quantity": part["quantity"],
                    "inherited_status": child_status,
                })
            self.connection.execute(
                "UPDATE batches SET remaining_quantity_text=?,updated_at=? WHERE batch_id=?",
                (
                    format(Decimal(parent["remaining_quantity_text"]) - total, "f"),
                    now,
                    parent_id,
                ),
            )
            self.connection.execute(
                "INSERT INTO split_operations(split_id,parent_id,idempotency_key,request_sha256,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (split_id, parent_id, idempotency_key, request_digest, actor_id, now),
            )
            response = {
                "split_id": split_id,
                "parent_id": parent_id,
                "child_ids": child_ids,
                "status": child_status,
            }
            self._store_replay(f"split:{parent_id}", idempotency_key, request_digest, response)
            self._audit("batch", parent_id, "batch.split", actor_id, {
                "split_id": split_id,
                "child_ids": child_ids,
                "quantities": [part["quantity"] for part in parts],
                "inherited_status": child_status,
            })
        return response

    def _copy_declaration(
        self, declaration: sqlite3.Row, batch_id: str, actor_id: str, now: str
    ) -> int:
        fields = {
            "source": json.loads(declaration["source_json"]),
            "composition": json.loads(declaration["composition_json"]),
            "basis_doc": (
                f"继承自批次 {declaration['batch_id']} 声明 #{declaration['revision_no']}；"
                f"原始依据：{declaration['basis_doc']}"
            ),
        }
        return self._insert_declaration(batch_id, 1, None, fields, actor_id, now)

    def merge_batches(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.register")
        merge_id = self._text(raw, "merge_id")
        idempotency_key = self._text(raw, "idempotency_key")
        contributions_raw = raw.get("parents")
        if not isinstance(contributions_raw, list) or len(contributions_raw) < 2:
            raise ValidationFailed("合批至少要指定两个父批次")
        contributions: list[dict[str, Any]] = []
        seen_parents: set[str] = set()
        for item in contributions_raw:
            if not isinstance(item, dict):
                raise ValidationFailed("parents 条目必须是对象")
            parent_id = self._text(item, "batch_id")
            if parent_id in seen_parents:
                raise ValidationFailed(f"父批次重复: {parent_id}")
            seen_parents.add(parent_id)
            amount = Decimal(quantity_text(item.get("quantity"), "quantity"))
            contributions.append({"batch_id": parent_id, "quantity": amount})
        child_raw = raw.get("child")
        if not isinstance(child_raw, dict):
            raise ValidationFailed("child 必须是合批结果批次声明")
        child_id = self._text(child_raw, "batch_id")
        material_type = self._text(child_raw, "material_type")
        unit = self._text(child_raw, "unit")
        child_quantity = Decimal(quantity_text(child_raw.get("quantity"), "quantity"))
        total = sum((item["quantity"] for item in contributions), Decimal("0"))
        if child_quantity != total:
            raise ValidationFailed("合批子批次数量必须等于父批次投入数量之和")
        fields = self._declaration_fields(child_raw)
        request_digest = digest(raw)
        with self._unit():
            replayed = self._replay(f"merge:{child_id}", idempotency_key, request_digest)
            if replayed is not None:
                return replayed
            parents = [self._batch_row(item["batch_id"]) for item in contributions]
            quarantined_parents: list[str] = []
            for parent, contribution in zip(parents, contributions):
                self._require_active(parent)
                if contribution["quantity"] > Decimal(parent["remaining_quantity_text"]):
                    raise ValidationFailed(f"父批次 {parent['batch_id']} 结余数量不足")
                if parent["material_type"] != material_type or parent["unit"] != unit:
                    raise ValidationFailed(
                        f"父批次 {parent['batch_id']} 的材料类型或单位与合批结果不一致"
                    )
                if parent["status"] == _QUARANTINED:
                    quarantined_parents.append(parent["batch_id"])
            if self.connection.execute(
                "SELECT 1 FROM batches WHERE batch_id=?", (child_id,)
            ).fetchone():
                raise Conflict(f"合批结果批次编号已存在: {child_id}")
            now = self._now()
            # 混合形成新材料实体，一律回到待检状态；任一父批次被隔离则粘性继承隔离。
            child_status = _QUARANTINED if quarantined_parents else "registered"
            self._insert_batch(
                child_id, material_type, format(total, "f"), unit, child_status, actor_id, now
            )
            declaration_id = self._insert_declaration(child_id, 1, None, fields, actor_id, now)
            self.connection.execute(
                "UPDATE batches SET current_declaration_id=? WHERE batch_id=?",
                (declaration_id, child_id),
            )
            for parent, contribution in zip(parents, contributions):
                revision_no = self.connection.execute(
                    "SELECT revision_no FROM declarations WHERE declaration_id=?",
                    (parent["current_declaration_id"],),
                ).fetchone()["revision_no"]
                self.connection.execute(
                    "INSERT INTO lineage_edges(parent_id,child_id,operation_kind,operation_id,"
                    "quantity_text,declaration_id,declaration_revision,created_at) "
                    "VALUES(?,?,'merge',?,?,?,?,?)",
                    (
                        parent["batch_id"],
                        child_id,
                        merge_id,
                        format(contribution["quantity"], "f"),
                        parent["current_declaration_id"],
                        revision_no,
                        now,
                    ),
                )
                self.connection.execute(
                    "UPDATE batches SET remaining_quantity_text=?,updated_at=? WHERE batch_id=?",
                    (
                        format(
                            Decimal(parent["remaining_quantity_text"]) - contribution["quantity"],
                            "f",
                        ),
                        now,
                        parent["batch_id"],
                    ),
                )
            if quarantined_parents:
                self._inherit_quarantine_decision(
                    child_id,
                    f"合批父批次处于隔离状态: {', '.join(quarantined_parents)}（合批 {merge_id}）",
                    actor_id,
                    now,
                )
            self.connection.execute(
                "INSERT INTO merge_operations(merge_id,child_id,idempotency_key,request_sha256,"
                "created_by,created_at) VALUES(?,?,?,?,?,?)",
                (merge_id, child_id, idempotency_key, request_digest, actor_id, now),
            )
            response = {
                "merge_id": merge_id,
                "child_id": child_id,
                "parent_ids": [item["batch_id"] for item in contributions],
                "status": child_status,
            }
            self._store_replay(f"merge:{child_id}", idempotency_key, request_digest, response)
            self._audit("batch", child_id, "batch.merged", actor_id, {
                "merge_id": merge_id,
                "parent_ids": [item["batch_id"] for item in contributions],
                "quantities": [format(item["quantity"], "f") for item in contributions],
                "inherited_status": child_status,
                "quarantined_parents": quarantined_parents,
            })
        return response

    # ------------------------------------------------------------- 不可逆处置

    def dispose(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "disposition.write")
        batch_id = self._text(raw, "batch_id")
        kind = self._text(raw, "kind")
        if kind not in _DISPOSITION_KINDS:
            raise ValidationFailed(f"kind 必须是 {_DISPOSITION_KINDS} 之一")
        method = self._text(raw, "method")
        facility = self._text(raw, "facility")
        document_ref = self._text(raw, "document_ref")
        idempotency_key = self._text(raw, "idempotency_key")
        quantity = quantity_text(raw.get("quantity"), "quantity")
        request_digest = digest(raw)
        with self._unit():
            replayed = self._replay(f"disposition:{batch_id}", idempotency_key, request_digest)
            if replayed is not None:
                return replayed
            batch = self._batch_row(batch_id)
            self._require_active(batch)
            if Decimal(quantity) != Decimal(batch["remaining_quantity_text"]):
                raise ValidationFailed("不可逆处置必须按批次当前结余数量整体执行")
            now = self._now()
            self.connection.execute(
                "INSERT INTO dispositions(batch_id,kind,quantity_text,method,facility,document_ref,"
                "idempotency_key,operated_by,operated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (batch_id, kind, quantity, method, facility, document_ref, idempotency_key, actor_id, now),
            )
            self.connection.execute(
                "UPDATE batches SET status='disposed',disposition_kind=?,updated_at=? WHERE batch_id=?",
                (kind, now, batch_id),
            )
            response = {"batch_id": batch_id, "status": _DISPOSED, "disposition_kind": kind}
            self._store_replay(f"disposition:{batch_id}", idempotency_key, request_digest, response)
            self._audit("batch", batch_id, "batch.disposed", actor_id, {
                "kind": kind,
                "quantity": quantity,
                "method": method,
                "facility": facility,
                "document_ref": document_ref,
            })
        return response

    # ------------------------------------------------------------- 清单导入

    def import_manifest(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """批量导入批次登记。

        隔离具有粘性：已隔离批次即使再次出现在外部清单中，也只会被
        跳过并留痕，状态绝不被重置；其他已存在批次直接冲突并整体回
        滚，防止重复导入让材料重新进入可用状态。
        """
        self._require(actor_id, "manifest.import")
        idempotency_key = self._text(raw, "idempotency_key")
        source_ref = self._text(raw, "source_ref")
        entries = raw.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ValidationFailed("entries 不能为空")
        request_digest = digest(raw)
        with self._unit():
            replayed = self._replay("manifest", idempotency_key, request_digest)
            if replayed is not None:
                return replayed
            now = self._now()
            registered: list[str] = []
            preserved: list[str] = []
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValidationFailed("清单条目必须是对象")
                batch_id = self._text(entry, "batch_id")
                existing = self.connection.execute(
                    "SELECT status FROM batches WHERE batch_id=?", (batch_id,)
                ).fetchone()
                if existing is not None:
                    if existing["status"] == _QUARANTINED:
                        preserved.append(batch_id)
                        self._audit("batch", batch_id, "manifest.quarantine_preserved", actor_id, {
                            "source_ref": source_ref,
                        })
                        continue
                    raise Conflict(f"批次 {batch_id} 已存在，禁止重复导入")
                material_type = self._text(entry, "material_type")
                unit = self._text(entry, "unit")
                quantity = quantity_text(entry.get("quantity"), "quantity")
                fields = self._declaration_fields(entry)
                self._insert_batch(
                    batch_id, material_type, quantity, unit, "registered", actor_id, now
                )
                declaration_id = self._insert_declaration(batch_id, 1, None, fields, actor_id, now)
                self.connection.execute(
                    "UPDATE batches SET current_declaration_id=? WHERE batch_id=?",
                    (declaration_id, batch_id),
                )
                registered.append(batch_id)
                self._audit("batch", batch_id, "manifest.entry_registered", actor_id, {
                    "source_ref": source_ref,
                    "declaration_id": declaration_id,
                })
            summary = {
                "registered_count": len(registered),
                "preserved_quarantine_count": len(preserved),
                "registered": registered,
                "preserved_quarantine": preserved,
            }
            cursor = self.connection.execute(
                "INSERT INTO manifest_imports(source_ref,idempotency_key,content_sha256,summary_json,"
                "imported_by,imported_at) VALUES(?,?,?,?,?,?)",
                (source_ref, idempotency_key, request_digest, canonical_json(summary), actor_id, now),
            )
            import_id = int(cursor.lastrowid)
            response = {"import_id": import_id, "source_ref": source_ref, **summary}
            self._store_replay("manifest", idempotency_key, request_digest, response)
            self._audit("manifest", str(import_id), "manifest.imported", actor_id, {
                "source_ref": source_ref,
                **summary,
            })
        return response

    # ------------------------------------------------------------- 追溯视图

    def trace(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """按批次还原每次交接、检测、决定、处置和谱系边。"""
        self._require(actor_id, "trace.read")
        batch = self._batch_row(batch_id)
        tests = []
        for row in self.connection.execute(
            "SELECT test_id,method,instrument,results_json,verdict,basis_doc,recorded_by,recorded_at "
            "FROM tests WHERE batch_id=? ORDER BY test_id",
            (batch_id,),
        ).fetchall():
            item = dict(row)
            item["results"] = json.loads(item.pop("results_json"))
            tests.append(item)
        decisions = [
            dict(row)
            for row in self.connection.execute(
                "SELECT decision_id,decision,reason,test_id,decided_by,decided_at "
                "FROM decisions WHERE batch_id=? ORDER BY decision_id",
                (batch_id,),
            ).fetchall()
        ]
        custody = [
            dict(row)
            for row in self.connection.execute(
                "SELECT custody_id,handoff_type,from_party,to_party,document_ref,"
                "observed_quantity_text,note,handled_by,occurred_at "
                "FROM custody_events WHERE batch_id=? ORDER BY custody_id",
                (batch_id,),
            ).fetchall()
        ]
        disposition = self.connection.execute(
            "SELECT kind,quantity_text,method,facility,document_ref,operated_by,operated_at "
            "FROM dispositions WHERE batch_id=?",
            (batch_id,),
        ).fetchone()
        declarations = [
            {
                "declaration_id": row["declaration_id"],
                "revision_no": row["revision_no"],
                "supersedes_id": row["supersedes_id"],
                "source": json.loads(row["source_json"]),
                "composition": json.loads(row["composition_json"]),
                "basis_doc": row["basis_doc"],
                "declared_by": row["declared_by"],
                "declared_at": row["declared_at"],
            }
            for row in self.connection.execute(
                "SELECT * FROM declarations WHERE batch_id=? ORDER BY declaration_id", (batch_id,)
            ).fetchall()
        ]
        parents = [
            dict(row)
            for row in self.connection.execute(
                "SELECT parent_id,operation_kind,operation_id,quantity_text,declaration_revision "
                "FROM lineage_edges WHERE child_id=? ORDER BY edge_id",
                (batch_id,),
            ).fetchall()
        ]
        children = [
            dict(row)
            for row in self.connection.execute(
                "SELECT child_id,operation_kind,operation_id,quantity_text,declaration_revision "
                "FROM lineage_edges WHERE parent_id=? ORDER BY edge_id",
                (batch_id,),
            ).fetchall()
        ]
        events = [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "event_hash": row["event_hash"],
            }
            for row in self.connection.execute(
                "SELECT * FROM audit_events WHERE entity_type='batch' AND entity_id=? ORDER BY event_id",
                (batch_id,),
            ).fetchall()
        ]
        return {
            "batch": dict(batch),
            "declarations": declarations,
            "tests": tests,
            "decisions": decisions,
            "custody": custody,
            "disposition": None if disposition is None else dict(disposition),
            "parents": parents,
            "children": children,
            "events": events,
        }

    def _descendant_rows(self, batch_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            """
            WITH RECURSIVE descendants(batch_id, depth) AS (
                SELECT child_id, 1 FROM lineage_edges WHERE parent_id=?
                UNION
                SELECT e.child_id, d.depth + 1
                FROM lineage_edges e JOIN descendants d ON e.parent_id = d.batch_id
            )
            SELECT d.batch_id, min(d.depth) AS depth, b.status, b.disposition_kind
            FROM descendants d JOIN batches b ON b.batch_id = d.batch_id
            GROUP BY d.batch_id
            ORDER BY depth, d.batch_id
            """,
            (batch_id,),
        ).fetchall()

    def lineage(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """派生影响视图：父批次更正时看清受影响批次与已不可逆处置的批次。"""
        self._require(actor_id, "trace.read")
        self._batch_row(batch_id)
        descendants = [dict(row) for row in self._descendant_rows(batch_id)]
        ancestors = [
            dict(row)
            for row in self.connection.execute(
                """
                WITH RECURSIVE ancestors(batch_id, depth) AS (
                    SELECT parent_id, 1 FROM lineage_edges WHERE child_id=?
                    UNION
                    SELECT e.parent_id, a.depth + 1
                    FROM lineage_edges e JOIN ancestors a ON e.child_id = a.batch_id
                )
                SELECT a.batch_id, min(a.depth) AS depth, b.status, b.disposition_kind
                FROM ancestors a JOIN batches b ON b.batch_id = a.batch_id
                GROUP BY a.batch_id
                ORDER BY depth, a.batch_id
                """,
                (batch_id,),
            ).fetchall()
        ]
        return {
            "batch_id": batch_id,
            "ancestors": ancestors,
            "descendants": descendants,
            "descendant_count": len(descendants),
            "disposed_descendants": [
                item["batch_id"] for item in descendants if item["status"] == _DISPOSED
            ],
            "quarantined_descendants": [
                item["batch_id"] for item in descendants if item["status"] == _QUARANTINED
            ],
        }

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
