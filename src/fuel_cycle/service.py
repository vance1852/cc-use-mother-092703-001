"""核燃料循环批次监管的领域用例。

覆盖来源与成分声明登记、分批/合批、检测复核、有权人员放行/隔离决定、
运输交接、不可逆处置与批次谱系追溯。所有业务事实只追加，审计事件成
全局哈希链；重复导入通过来源凭证唯一约束与幂等键双重防护，绝不会让
已隔离材料重新进入可用状态。
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat, parse_utc
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "operator": {
        "declaration.register", "transform.write", "trace.read",
    },
    "analyst": {"inspection.record", "trace.read"},
    "quality": {
        "declaration.correct", "decision.write", "disposal.write",
        "inspection.record", "trace.read",
    },
    "custodian": {"transfer.dispatch", "transfer.receive", "trace.read"},
    "auditor": {"trace.read", "audit.verify"},
}

SOURCE_TYPES = {
    "mining", "milling", "conversion", "enrichment", "fabrication",
    "recovery", "derived", "imported",
}
ACTIVE_STATES = {"declared", "released", "quarantined"}
UNIT_MAX = 16


class FuelCycleService:
    """在单个 SQLite 连接上提供全部批次监管操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    # ------------------------------------------------------------------ 用户

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM fuel_users WHERE user_id=?",
            (user_id,),
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
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO fuel_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------ 审计

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = content_digest([body])
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def verify_chain(self, actor_id: str) -> dict[str, Any]:
        """重放全局审计哈希链，供审计人员发现任何被篡改的历史。"""

        self._require(actor_id, "audit.verify")
        rows = self.connection.execute(
            "SELECT event_id, entity_type, entity_id, event_type, actor_id, payload_json, "
            "previous_hash, event_hash, created_at FROM audit_events ORDER BY event_id"
        ).fetchall()
        previous_hash = "0" * 64
        for row in rows:
            if row["previous_hash"] != previous_hash:
                return {"valid": False, "broken_at": row["event_id"], "event_count": len(rows)}
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            if content_digest([body]) != row["event_hash"]:
                return {"valid": False, "broken_at": row["event_id"], "event_count": len(rows)}
            previous_hash = row["event_hash"]
        return {"valid": True, "event_count": len(rows), "head_hash": previous_hash}

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def _quantity(value: object, field: str, *, positive: bool = True) -> Decimal:
        if isinstance(value, bool):
            raise ValidationFailed(f"{field} 必须是数值")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValidationFailed(f"{field} 必须是十进制数值") from exc
        if not result.is_finite():
            raise ValidationFailed(f"{field} 必须是有限数值")
        if positive and result <= 0:
            raise ValidationFailed(f"{field} 必须大于零")
        return result

    @staticmethod
    def _text(value: object, field: str, maximum: int = 256) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValidationFailed(f"{field} 不能为空")
        result = value.strip()
        if len(result) > maximum:
            raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
        return result

    def _components(self, raw: object) -> list[dict[str, str]]:
        if not isinstance(raw, list) or not raw:
            raise ValidationFailed("成分声明 components 必须是非空数组")
        components: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping) or "element" not in item or "weight_fraction" not in item:
                raise ValidationFailed("每个成分需要 element 与 weight_fraction")
            element = self._text(item["element"], "成分元素", 64)
            if element in seen:
                raise ValidationFailed(f"成分 {element} 重复")
            seen.add(element)
            fraction = self._quantity(item["weight_fraction"], f"成分 {element} 质量分数", positive=False)
            if fraction < 0 or fraction > 1:
                raise ValidationFailed(f"成分 {element} 质量分数必须在 0 到 1 之间")
            components.append({"element": element, "weight_fraction": format(fraction, "f")})
        return components

    def _results(self, raw: object) -> dict[str, str]:
        if not isinstance(raw, Mapping) or not raw:
            raise ValidationFailed("检测结果 results 必须是非空对象")
        results: dict[str, str] = {}
        for key, value in raw.items():
            key = self._text(key, "检测参数名", 64)
            number = self._quantity(value, f"检测值 {key}", positive=False)
            results[key] = format(number, "f")
        return results

    def _limits(self, raw: object) -> dict[str, dict[str, str]]:
        if not isinstance(raw, Mapping):
            raise ValidationFailed("限值 limits 必须是对象")
        limits: dict[str, dict[str, str]] = {}
        for key, value in raw.items():
            key = self._text(key, "限值参数名", 64)
            if not isinstance(value, Mapping):
                raise ValidationFailed(f"限值 {key} 必须含 min/max")
            bound: dict[str, str] = {}
            minimum = value.get("min")
            maximum = value.get("max")
            if minimum is not None:
                bound["min"] = format(self._quantity(minimum, f"{key} 下限", positive=False), "f")
            if maximum is not None:
                bound["max"] = format(self._quantity(maximum, f"{key} 上限", positive=False), "f")
            if not bound:
                raise ValidationFailed(f"限值 {key} 至少需要 min 或 max")
            if "min" in bound and "max" in bound and Decimal(bound["min"]) > Decimal(bound["max"]):
                raise ValidationFailed(f"限值 {key} 下限不能大于上限")
            limits[key] = bound
        return limits

    @staticmethod
    def _verdict(results: Mapping[str, str], limits: Mapping[str, Mapping[str, str]]) -> str | None:
        if not limits:
            return None
        for key, bound in limits.items():
            if key not in results:
                return "inconclusive"
            value = Decimal(results[key])
            if "min" in bound and value < Decimal(bound["min"]):
                return "fail"
            if "max" in bound and value > Decimal(bound["max"]):
                return "fail"
        return "pass"

    def _batch(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound(f"批次不存在: {batch_id}")
        return row

    def _latest_declaration(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM source_declarations WHERE batch_id=? ORDER BY version DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"批次缺少来源声明: {batch_id}")
        return row

    def _lineage_basis(self, batch_id: str) -> dict[str, int]:
        """返回本批次及其全部祖先当前的声明版本快照。"""

        basis: dict[str, int] = {}
        stack = [batch_id]
        while stack:
            current = stack.pop()
            row = self.connection.execute(
                "SELECT declaration_version FROM batches WHERE batch_id=?", (current,)
            ).fetchone()
            if row is None:
                continue
            basis[current] = row["declaration_version"]
            parents = self.connection.execute(
                "SELECT i.batch_id AS parent_id FROM transform_outputs o "
                "JOIN transform_inputs i ON i.transform_id=o.transform_id "
                "WHERE o.batch_id=?",
                (current,),
            ).fetchall()
            for parent in parents:
                if parent["parent_id"] not in basis:
                    stack.append(parent["parent_id"])
        return basis

    @staticmethod
    def _lineage_matches(snapshot_json: str | None, current: Mapping[str, int]) -> bool:
        if snapshot_json is None:
            return False
        snapshot = json.loads(snapshot_json)
        return all(current.get(batch_id) == version for batch_id, version in snapshot.items())

    def _descendants(self, batch_id: str) -> list[str]:
        """向下遍历谱系，返回全部派生批次编号。"""

        result: list[str] = []
        seen = {batch_id}
        stack = [batch_id]
        while stack:
            current = stack.pop()
            children = self.connection.execute(
                "SELECT o.batch_id AS child_id FROM transform_inputs i "
                "JOIN transform_outputs o ON o.transform_id=i.transform_id "
                "WHERE i.batch_id=?",
                (current,),
            ).fetchall()
            for child in children:
                if child["child_id"] not in seen:
                    seen.add(child["child_id"])
                    result.append(child["child_id"])
                    stack.append(child["child_id"])
        return result

    def _idempotent(self, scope: str, key: str, digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256, response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    # ---------------------------------------------------------- 来源与成分声明

    def register_declaration(
        self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str
    ) -> dict[str, Any]:
        """登记新批次的来源与成分声明（材料进入账本的唯一入口）。"""

        actor = self._require(actor_id, "declaration.register")
        key = self._text(idempotency_key, "Idempotency-Key", 128)
        normalized = self._normalize_declaration(raw)
        digest = content_digest([normalized, key])
        cached = self._idempotent("declaration.register", key, digest)
        if cached is not None:
            # 重放绝不触碰批次状态——即使批次此后已被隔离或处置。
            return cached
        batch_id = normalized["batch_id"]
        if not isinstance(batch_id, str) or not batch_id.strip():
            raise ValidationFailed("batch_id 不能为空")
        response: dict[str, Any]
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                self.connection.execute(
                    "INSERT INTO batches(batch_id,material_type,quantity,unit,status,declaration_version,"
                    "status_reason,created_by,created_at,updated_at) VALUES(?,?,?,?,'declared',1,?,?,?,?)",
                    (
                        batch_id, normalized["material_type"], normalized["quantity"],
                        normalized["unit"], "首次来源声明登记", actor["user_id"], now, now,
                    ),
                )
                cursor = self.connection.execute(
                    "INSERT INTO source_declarations(batch_id,version,source_type,source_reference,supplier,"
                    "origin_doc_ref,material_type,quantity,unit,components_json,remarks,received_at,"
                    "basis_sha256,declared_by,declared_at,supersedes_id) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        batch_id, 1, normalized["source_type"], normalized["source_reference"],
                        normalized["supplier"], normalized["origin_doc_ref"],
                        normalized["material_type"], normalized["quantity"], normalized["unit"],
                        canonical_json(normalized["components"]), normalized.get("remarks"),
                        normalized["received_at"], digest, actor["user_id"], now,
                    ),
                )
                self.connection.execute(
                    "INSERT INTO source_registry(source_type,source_reference,batch_id,registered_at) "
                    "VALUES(?,?,?,?)",
                    (normalized["source_type"], normalized["source_reference"], batch_id, now),
                )
                response = {
                    "batch_id": batch_id,
                    "declaration_id": cursor.lastrowid,
                    "declaration_version": 1,
                    "status": "declared",
                    "replayed": False,
                }
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    ("declaration.register", key, digest, canonical_json(response), now),
                )
                self._audit("batch", batch_id, "declaration.registered", actor["user_id"], {
                    "source_type": normalized["source_type"],
                    "source_reference": normalized["source_reference"],
                    "version": 1,
                    "basis_sha256": digest,
                })
        except sqlite3.IntegrityError as exc:
            # 来源凭证全局唯一：同一份外部凭证的重复导入只会得到冲突，
            # 不会新建批次，更不会复活任何已隔离/已处置材料。
            raise Conflict("来源凭证已登记或批次编号冲突，重复导入被拒绝") from exc
        return response

    def correct_declaration(self, actor_id: str, batch_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """以新版本更正来源/成分声明；旧版本与依据摘要原样保留。"""

        actor = self._require(actor_id, "declaration.correct")
        normalized = self._normalize_declaration(raw)
        with transaction(self.connection, immediate=True):
            batch = self._batch(batch_id)
            if batch["status"] == "disposed":
                raise InvalidState("已不可逆处置的批次不能更正声明")
            previous = self._latest_declaration(batch_id)
            if normalized["source_type"] != previous["source_type"]:
                raise ValidationFailed("更正不得改变来源类型")
            if normalized["source_reference"] != previous["source_reference"]:
                raise ValidationFailed("更正不得改变来源凭证编号")
            if normalized["unit"] != batch["unit"]:
                raise ValidationFailed("更正不得改变计量单位")
            new_version = previous["version"] + 1
            now = self._now()
            digest = content_digest([normalized, f"correct:{new_version}"])
            cursor = self.connection.execute(
                "INSERT INTO source_declarations(batch_id,version,source_type,source_reference,supplier,"
                "origin_doc_ref,material_type,quantity,unit,components_json,remarks,received_at,"
                "basis_sha256,declared_by,declared_at,supersedes_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, new_version, normalized["source_type"], normalized["source_reference"],
                    normalized["supplier"], normalized["origin_doc_ref"],
                    normalized["material_type"], normalized["quantity"], normalized["unit"],
                    canonical_json(normalized["components"]), normalized.get("remarks"),
                    normalized["received_at"], digest, actor["user_id"], now, previous["declaration_id"],
                ),
            )
            self.connection.execute(
                "UPDATE batches SET material_type=?,declaration_version=?,updated_at=? WHERE batch_id=?",
                (normalized["material_type"], new_version, now, batch_id),
            )
            # 先更新版本，再按快照撤回自身及派生批次基于旧信息的放行。
            invalidated = self._invalidate_releases_after_correction(
                batch_id, new_version, actor["user_id"], now
            )
            self._audit("batch", batch_id, "declaration.corrected", actor["user_id"], {
                "version": new_version,
                "supersedes_id": previous["declaration_id"],
                "basis_sha256": digest,
                "invalidated_batches": invalidated,
            })
        return self.get_declaration_history(actor_id, batch_id)

    def _normalize_declaration(self, raw: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValidationFailed("声明内容必须是 JSON 对象")
        source_type = self._text(raw.get("source_type"), "source_type", 32)
        if source_type not in SOURCE_TYPES:
            raise ValidationFailed(f"未知来源类型: {source_type}")
        unit = self._text(raw.get("unit"), "unit", UNIT_MAX)
        quantity = self._quantity(raw.get("quantity"), "quantity")
        result = {
            "batch_id": self._text(raw.get("batch_id"), "batch_id", 64),
            "source_type": source_type,
            "source_reference": self._text(raw.get("source_reference"), "source_reference", 128),
            "supplier": self._text(raw.get("supplier"), "supplier", 128),
            "origin_doc_ref": self._text(raw.get("origin_doc_ref"), "origin_doc_ref", 128),
            "material_type": self._text(raw.get("material_type"), "material_type", 64),
            "quantity": format(quantity, "f"),
            "unit": unit,
            "components": self._components(raw.get("components")),
            "remarks": raw.get("remarks"),
            "received_at": parse_utc(raw.get("received_at"), "received_at"),
        }
        if result["remarks"] is not None:
            result["remarks"] = self._text(result["remarks"], "remarks", 1024)
        return result

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        return dict(self._batch(batch_id))

    def get_declaration_history(self, actor_id: str, batch_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "trace.read")
        self._batch(batch_id)
        rows = self.connection.execute(
            "SELECT * FROM source_declarations WHERE batch_id=? ORDER BY version", (batch_id,)
        ).fetchall()
        return [self._declaration_dict(row) for row in rows]

    @staticmethod
    def _declaration_dict(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        data["components"] = json.loads(data.pop("components_json"))
        return data

    # ------------------------------------------------------------ 分批与合批

    def split_batch(
        self,
        actor_id: str,
        transform_id: str,
        batch_id: str,
        parts: Sequence[Mapping[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        """将一个已放行批次分批；输出为新批次（声明类型 derived），须重新检测放行。"""

        actor = self._require(actor_id, "transform.write")
        self._validate_transform_id(transform_id)
        parent = self._batch(batch_id)
        if parent["status"] != "released":
            raise InvalidState("只有已放行批次可以分批，隔离材料不得拆分")
        if not parts:
            raise ValidationFailed("分批至少需要一个输出份额")
        outputs: list[dict[str, Any]] = []
        total = Decimal(0)
        for ordinal, part in enumerate(parts, start=1):
            quantity = self._quantity(part.get("quantity") if isinstance(part, Mapping) else part, "分批数量")
            unit = self._text(
                part.get("unit", parent["unit"]) if isinstance(part, Mapping) else parent["unit"],
                "分批单位", UNIT_MAX,
            )
            if unit != parent["unit"]:
                raise ValidationFailed("分批单位必须与父批次一致")
            child_id = self._text(part.get("batch_id"), "分批输出 batch_id", 64) if isinstance(part, Mapping) else ""
            if not child_id:
                raise ValidationFailed("每个分批输出都需要 batch_id")
            total += quantity
            outputs.append({"ordinal": ordinal, "batch_id": child_id, "quantity": quantity, "unit": unit})
        if len({o["batch_id"] for o in outputs}) != len(outputs):
            raise ValidationFailed("分批输出批次号重复")
        for output in outputs:
            if self.connection.execute(
                "SELECT 1 FROM batches WHERE batch_id=?", (output["batch_id"],)
            ).fetchone():
                raise Conflict(f"输出批次已存在: {output['batch_id']}")
        remaining = Decimal(parent["quantity"]) - total
        if remaining < 0:
            raise ValidationFailed("分批总量超过父批次当前数量")
        with transaction(self.connection, immediate=True):
            self._insert_transform_header(actor, transform_id, "split", note)
            now = self._now()
            for output in outputs:
                self._spawn_derived_batch(actor, transform_id, output, [parent], now)
            self._insert_transform_links(
                transform_id, [(batch_id, total, parent["unit"])], outputs
            )
            parent_status = "exhausted" if remaining == 0 else "released"
            self.connection.execute(
                "UPDATE batches SET status=?,quantity=?,updated_at=? WHERE batch_id=?",
                (parent_status, format(remaining, "f"), now, batch_id),
            )
            self._audit("batch", batch_id, "batch.split", actor["user_id"], {
                "transform_id": transform_id,
                "outputs": [{"batch_id": o["batch_id"], "quantity": o["quantity"]} for o in outputs],
                "remaining": format(remaining, "f"),
            })
        return self._transform_view(transform_id)

    def merge_batches(
        self,
        actor_id: str,
        transform_id: str,
        parent_ids: Sequence[str],
        outputs: Sequence[Mapping[str, Any]],
        note: str = "",
    ) -> dict[str, Any]:
        """将多个同材质、同单位的已放行批次合批；隔离批次不得参与（防止洗白）。"""

        actor = self._require(actor_id, "transform.write")
        self._validate_transform_id(transform_id)
        if len(parent_ids) < 2:
            raise ValidationFailed("合批至少需要两个输入批次")
        if len(parent_ids) != len(set(parent_ids)):
            raise ValidationFailed("合批输入批次重复")
        if not outputs:
            raise ValidationFailed("合批至少需要一个输出批次")
        parents = [self._batch(pid) for pid in parent_ids]
        for parent in parents:
            if parent["status"] != "released":
                raise InvalidState(f"批次 {parent['batch_id']} 状态为 {parent['status']}，只有已放行批次可以合批")
        material_type = parents[0]["material_type"]
        unit = parents[0]["unit"]
        for parent in parents[1:]:
            if parent["material_type"] != material_type or parent["unit"] != unit:
                raise ValidationFailed("合批要求材质与计量单位完全一致")
        input_total = sum((Decimal(p["quantity"]) for p in parents), Decimal(0))
        normalized_outputs: list[dict[str, Any]] = []
        output_total = Decimal(0)
        for ordinal, raw in enumerate(outputs, start=1):
            if not isinstance(raw, Mapping):
                raise ValidationFailed("合批输出必须是对象")
            child_id = self._text(raw.get("batch_id"), "合批输出 batch_id", 64)
            quantity = self._quantity(raw.get("quantity"), "合批输出数量")
            out_unit = self._text(raw.get("unit", unit), "合批输出单位", UNIT_MAX)
            if out_unit != unit:
                raise ValidationFailed("合批输出单位必须与输入一致")
            output_total += quantity
            normalized_outputs.append({"ordinal": ordinal, "batch_id": child_id, "quantity": quantity, "unit": out_unit})
        if len({o["batch_id"] for o in normalized_outputs}) != len(normalized_outputs):
            raise ValidationFailed("合批输出批次号重复")
        for output in normalized_outputs:
            if self.connection.execute(
                "SELECT 1 FROM batches WHERE batch_id=?", (output["batch_id"],)
            ).fetchone():
                raise Conflict(f"输出批次已存在: {output['batch_id']}")
        if output_total != input_total:
            raise ValidationFailed("合批输出总量必须等于输入总量（物料守恒）")
        with transaction(self.connection, immediate=True):
            self._insert_transform_header(actor, transform_id, "merge", note)
            now = self._now()
            for output in normalized_outputs:
                self._spawn_derived_batch(actor, transform_id, output, parents, now)
            self._insert_transform_links(
                transform_id,
                [(p["batch_id"], Decimal(p["quantity"]), p["unit"]) for p in parents],
                normalized_outputs,
            )
            for parent in parents:
                self.connection.execute(
                    "UPDATE batches SET status='exhausted',quantity='0',updated_at=? WHERE batch_id=?",
                    (now, parent["batch_id"]),
                )
            self._audit("batch", normalized_outputs[0]["batch_id"], "batch.merged", actor["user_id"], {
                "transform_id": transform_id,
                "inputs": list(parent_ids),
                "outputs": [{"batch_id": o["batch_id"], "quantity": o["quantity"]} for o in normalized_outputs],
            })
        return self._transform_view(transform_id)

    @staticmethod
    def _validate_transform_id(transform_id: str) -> None:
        if not isinstance(transform_id, str) or not transform_id.strip():
            raise ValidationFailed("transform_id 不能为空")

    def _insert_transform_header(self, actor: sqlite3.Row, transform_id: str, kind: str, note: str) -> None:
        try:
            self.connection.execute(
                "INSERT INTO material_transforms(transform_id,kind,note,created_by,created_at) "
                "VALUES(?,?,?,?,?)",
                (transform_id, kind, note or None, actor["user_id"], self._now()),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"分批/合批作业编号已存在: {transform_id}") from exc

    def _insert_transform_links(
        self,
        transform_id: str,
        inputs: Sequence[tuple[str, Decimal, str]],
        outputs: Sequence[Mapping[str, Any]],
    ) -> None:
        # 子批次已先于链接创建，外键在链接写入时即可满足。
        for batch_id, quantity, unit in inputs:
            self.connection.execute(
                "INSERT INTO transform_inputs(transform_id,batch_id,quantity,unit) VALUES(?,?,?,?)",
                (transform_id, batch_id, format(quantity, "f"), unit),
            )
        for output in outputs:
            self.connection.execute(
                "INSERT INTO transform_outputs(transform_id,batch_id,ordinal,quantity,unit) VALUES(?,?,?,?,?)",
                (transform_id, output["batch_id"], output["ordinal"], format(output["quantity"], "f"), output["unit"]),
            )

    def _spawn_derived_batch(
        self,
        actor: sqlite3.Row,
        transform_id: str,
        output: Mapping[str, Any],
        parents: Sequence[sqlite3.Row],
        now: str,
    ) -> None:
        material_type = parents[0]["material_type"]
        components = self._blended_components(parents)
        self.connection.execute(
            "INSERT INTO batches(batch_id,material_type,quantity,unit,status,declaration_version,"
            "status_reason,created_by,created_at,updated_at) VALUES(?,?,?,?,'declared',1,?,?,?,?)",
            (
                output["batch_id"], material_type, format(output["quantity"], "f"), output["unit"],
                f"由 {transform_id} 派生", actor["user_id"], now, now,
            ),
        )
        digest = content_digest([{"transform_id": transform_id, "ordinal": output["ordinal"]}])
        source_reference = f"{transform_id}#out-{output['ordinal']}"
        self.connection.execute(
            "INSERT INTO source_declarations(batch_id,version,source_type,source_reference,supplier,"
            "origin_doc_ref,material_type,quantity,unit,components_json,remarks,received_at,"
            "basis_sha256,declared_by,declared_at,supersedes_id) "
            "VALUES(?,1,'derived',?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                output["batch_id"], source_reference,
                "derived:" + ",".join(p["batch_id"] for p in parents), transform_id,
                material_type, format(output["quantity"], "f"), output["unit"],
                canonical_json(components), f"派生自分批/合批作业 {transform_id}", now,
                digest, actor["user_id"], now,
            ),
        )
        self.connection.execute(
            "INSERT INTO source_registry(source_type,source_reference,batch_id,registered_at) "
            "VALUES('derived',?,?,?)",
            (source_reference, output["batch_id"], now),
        )

    def _blended_components(self, parents: Sequence[sqlite3.Row]) -> list[dict[str, str]]:
        """按各父批次当前数量加权继承成分；元素取并集。"""

        declarations = [self._latest_declaration(p["batch_id"]) for p in parents]
        weights = [Decimal(p["quantity"]) for p in parents]
        total_weight = sum(weights, Decimal(0))
        elements = sorted({
            component["element"]
            for declaration in declarations
            for component in json.loads(declaration["components_json"])
        })
        blended: list[dict[str, str]] = []
        for element in elements:
            weighted = Decimal(0)
            for declaration, weight in zip(declarations, weights):
                fractions = {
                    item["element"]: Decimal(item["weight_fraction"])
                    for item in json.loads(declaration["components_json"])
                }
                weighted += fractions.get(element, Decimal(0)) * weight / total_weight
            blended.append({"element": element, "weight_fraction": format(weighted, "f")})
        return blended

    def _transform_view(self, transform_id: str) -> dict[str, Any]:
        header = self.connection.execute(
            "SELECT * FROM material_transforms WHERE transform_id=?", (transform_id,)
        ).fetchone()
        if header is None:
            raise NotFound(f"分批/合批作业不存在: {transform_id}")
        inputs = [dict(r) for r in self.connection.execute(
            "SELECT batch_id,quantity,unit FROM transform_inputs WHERE transform_id=? ORDER BY batch_id",
            (transform_id,),
        ).fetchall()]
        outputs = [dict(r) for r in self.connection.execute(
            "SELECT batch_id,ordinal,quantity,unit FROM transform_outputs WHERE transform_id=? ORDER BY ordinal",
            (transform_id,),
        ).fetchall()]
        return {**dict(header), "inputs": inputs, "outputs": outputs}

    # ---------------------------------------------------------------- 检测复核

    def record_inspection(
        self,
        actor_id: str,
        batch_id: str,
        test_type: str,
        lab: str,
        method: str,
        results: Mapping[str, Any],
        limits: Mapping[str, Any] | None = None,
        sampled_at: str | None = None,
        tested_at: str | None = None,
        verdict: str | None = None,
        conclusion: str = "",
    ) -> dict[str, Any]:
        actor = self._require(actor_id, "inspection.record")
        batch = self._batch(batch_id)
        if batch["status"] not in ACTIVE_STATES:
            raise InvalidState(f"批次状态 {batch['status']} 下不能登记检测")
        test_type = self._text(test_type, "test_type", 64)
        lab = self._text(lab, "lab", 128)
        method = self._text(method, "method", 128)
        normalized_results = self._results(results)
        normalized_limits = self._limits(limits or {})
        computed = self._verdict(normalized_results, normalized_limits)
        if computed is None:
            if verdict not in {"pass", "fail", "inconclusive"}:
                raise ValidationFailed("无判定限值时必须显式给出 verdict(pass/fail/inconclusive)")
            final_verdict = verdict
        else:
            final_verdict = computed
        sampled_text = parse_utc(sampled_at, "sampled_at") if sampled_at else self._now()
        tested_text = parse_utc(tested_at, "tested_at") if tested_at else self._now()
        if tested_text < sampled_text:
            raise ValidationFailed("检测时间不能早于取样时间")
        declaration = self._latest_declaration(batch_id)
        sequence_row = self.connection.execute(
            "SELECT coalesce(max(sequence_no),0)+1 AS next FROM inspections WHERE batch_id=?", (batch_id,)
        ).fetchone()
        sequence_no = sequence_row["next"]
        basis = {
            "batch_id": batch_id,
            "test_type": test_type,
            "lab": lab,
            "method": method,
            "results": normalized_results,
            "limits": normalized_limits,
            "basis_declaration_version": declaration["version"],
            "sampled_at": sampled_text,
            "tested_at": tested_text,
        }
        digest = content_digest([basis])
        with transaction(self.connection, immediate=True):
            lineage_basis = self._lineage_basis(batch_id)
            cursor = self.connection.execute(
                "INSERT INTO inspections(batch_id,sequence_no,lab,test_type,method,basis_declaration_version,"
                "lineage_basis_json,sampled_at,tested_at,results_json,limits_json,verdict,conclusion,"
                "basis_sha256,tested_by,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, sequence_no, lab, test_type, method, declaration["version"],
                    canonical_json(lineage_basis),
                    sampled_text, tested_text, canonical_json(normalized_results),
                    canonical_json(normalized_limits), final_verdict, conclusion or None,
                    digest, actor["user_id"], self._now(),
                ),
            )
            test_id = cursor.lastrowid
            self._audit("batch", batch_id, "inspection.recorded", actor["user_id"], {
                "test_id": test_id,
                "sequence_no": sequence_no,
                "verdict": final_verdict,
                "basis_declaration_version": declaration["version"],
                "lineage_basis": lineage_basis,
                "basis_sha256": digest,
            })
        return self.get_inspection(test_id)

    def get_inspection(self, test_id: int) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM inspections WHERE test_id=?", (test_id,)).fetchone()
        if row is None:
            raise NotFound(f"检测记录不存在: {test_id}")
        data = dict(row)
        data["results"] = json.loads(data.pop("results_json"))
        data["limits"] = json.loads(data.pop("limits_json"))
        if data.get("lineage_basis_json"):
            data["lineage_basis"] = json.loads(data.pop("lineage_basis_json"))
        else:
            data.pop("lineage_basis_json", None)
        return data

    def list_inspections(self, actor_id: str, batch_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "trace.read")
        self._batch(batch_id)
        rows = self.connection.execute(
            "SELECT * FROM inspections WHERE batch_id=? ORDER BY sequence_no", (batch_id,)
        ).fetchall()
        return [self.get_inspection(row["test_id"]) for row in rows]

    # ------------------------------------------------------------ 放行/隔离决定

    def record_decision(
        self, actor_id: str, batch_id: str, decision: str, reason: str, basis_test_id: int | None = None
    ) -> dict[str, Any]:
        """由有权人员记录放行或隔离决定；放行必须有基于当前声明版本的合格检测。"""

        actor = self._require(actor_id, "decision.write")
        if decision not in {"release", "quarantine"}:
            raise ValidationFailed("决定只能是 release 或 quarantine")
        reason = self._text(reason, "reason", 1024)
        with transaction(self.connection, immediate=True):
            batch = self._batch(batch_id)
            if batch["status"] == "disposed":
                raise InvalidState("已处置批次不能再作放行决定")
            if batch["status"] == "in_transit":
                raise InvalidState("批次在途，须完成接收后才能决定")
            declaration = self._latest_declaration(batch_id)
            test_row = None
            if basis_test_id is not None:
                test_row = self.connection.execute(
                    "SELECT * FROM inspections WHERE test_id=? AND batch_id=?",
                    (basis_test_id, batch_id),
                ).fetchone()
                if test_row is None:
                    raise NotFound("放行依据检测不存在或不属于该批次")
            if decision == "release":
                current_lineage = self._lineage_basis(batch_id)
                if test_row is None:
                    test_row = self.connection.execute(
                        "SELECT * FROM inspections WHERE batch_id=? ORDER BY sequence_no DESC LIMIT 1",
                        (batch_id,),
                    ).fetchone()
                if test_row is None:
                    raise InvalidState("放行必须依据检测复核结果")
                if test_row["verdict"] != "pass":
                    raise InvalidState("只有合格(pass)检测才能作为放行依据")
                if test_row["basis_declaration_version"] != declaration["version"]:
                    raise InvalidState(
                        "检测依据的声明版本已过期，请在信息更正后复检再放行"
                    )
                if not self._lineage_matches(test_row["lineage_basis_json"], current_lineage):
                    raise InvalidState(
                        "检测依据的上游批次声明已更正，请依据全部最新信息复检后再放行"
                    )
                if test_row["tested_by"] == actor["user_id"]:
                    raise Forbidden("放行人不能同时是检测人，必须四眼分离")
                effective_test_id = test_row["test_id"]
                lineage_snapshot = canonical_json(current_lineage)
                new_status = "released"
            else:
                effective_test_id = None if test_row is None else test_row["test_id"]
                lineage_snapshot = None
                new_status = "quarantined"
            sequence_row = self.connection.execute(
                "SELECT coalesce(max(sequence_no),0)+1 AS next FROM release_decisions WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            now = self._now()
            cursor = self.connection.execute(
                "INSERT INTO release_decisions(batch_id,sequence_no,decision,reason,basis_test_id,"
                "basis_declaration_version,lineage_basis_json,decided_by,decided_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    batch_id, sequence_row["next"], decision, reason, effective_test_id,
                    declaration["version"], lineage_snapshot, actor["user_id"], now,
                ),
            )
            self.connection.execute(
                "UPDATE batches SET status=?,status_reason=?,updated_at=? WHERE batch_id=?",
                (new_status, reason, now, batch_id),
            )
            self._audit("batch", batch_id, f"decision.{decision}", actor["user_id"], {
                "decision_id": cursor.lastrowid,
                "sequence_no": sequence_row["next"],
                "reason": reason,
                "basis_test_id": effective_test_id,
                "basis_declaration_version": declaration["version"],
            })
        return self.get_batch(batch_id)

    def list_decisions(self, actor_id: str, batch_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "trace.read")
        self._batch(batch_id)
        rows = self.connection.execute(
            "SELECT * FROM release_decisions WHERE batch_id=? ORDER BY sequence_no", (batch_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def _current_release_basis(self, batch_id: str) -> sqlite3.Row | None:
        """返回当前仍有效的放行决定（自身与全部祖先声明版本均未变更）。"""

        batch = self._batch(batch_id)
        latest = self.connection.execute(
            "SELECT * FROM release_decisions WHERE batch_id=? ORDER BY sequence_no DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        if latest is None or latest["decision"] != "release":
            return None
        if latest["basis_declaration_version"] != batch["declaration_version"]:
            return None
        if not self._lineage_matches(latest["lineage_basis_json"], self._lineage_basis(batch_id)):
            return None
        return latest

    def _invalidate_releases_after_correction(
        self, corrected_id: str, new_version: int, actor_id: str, now: str
    ) -> list[str]:
        """更正后撤回自身及派生批次基于旧信息的放行状态；在途批次由接收环节拦截。"""

        invalidated: list[str] = []
        targets = [corrected_id, *self._descendants(corrected_id)]
        for target_id in targets:
            target = self._batch(target_id)
            if target["status"] != "released":
                continue
            latest = self.connection.execute(
                "SELECT lineage_basis_json FROM release_decisions "
                "WHERE batch_id=? AND decision='release' ORDER BY sequence_no DESC LIMIT 1",
                (target_id,),
            ).fetchone()
            snapshot = json.loads(latest["lineage_basis_json"]) if latest and latest["lineage_basis_json"] else {}
            # 无法证明放行依据包含更正后版本（含旧数据缺失快照）时一律撤回。
            if snapshot.get(corrected_id) == new_version:
                continue
            self.connection.execute(
                "UPDATE batches SET status='quarantined',status_reason=?,updated_at=? WHERE batch_id=?",
                (f"上游批次 {corrected_id} 来源声明更正至 v{new_version}，原放行依据自动失效", now, target_id),
            )
            invalidated.append(target_id)
            self._audit("batch", target_id, "release.auto_invalidated", actor_id, {
                "corrected_batch_id": corrected_id,
                "new_declaration_version": new_version,
            })
        return invalidated

    # ---------------------------------------------------------------- 运输交接

    def dispatch_transfer(
        self,
        actor_id: str,
        transfer_id: str,
        batch_id: str,
        from_party: str,
        to_party: str,
        from_location: str,
        to_location: str,
        manifest_ref: str = "",
    ) -> dict[str, Any]:
        actor = self._require(actor_id, "transfer.dispatch")
        transfer_id = self._text(transfer_id, "transfer_id", 64)
        with transaction(self.connection, immediate=True):
            batch = self._batch(batch_id)
            basis = self._current_release_basis(batch_id)
            if batch["status"] != "released" or basis is None:
                raise InvalidState(
                    "只有持有当前有效放行依据的已放行批次可以发运；"
                    "隔离批次与放行依据因声明更正失效的批次不得发运"
                )
            now = self._now()
            try:
                self.connection.execute(
                    "INSERT INTO transfers(transfer_id,batch_id,from_party,to_party,from_location,to_location,"
                    "status,prior_status,dispatched_by,dispatched_at,manifest_ref) "
                    "VALUES(?,?,?,?,?,?,'dispatched','released',?,?,?)",
                    (
                        transfer_id, batch_id,
                        self._text(from_party, "from_party", 128),
                        self._text(to_party, "to_party", 128),
                        self._text(from_location, "from_location", 128),
                        self._text(to_location, "to_location", 128),
                        actor["user_id"], now, manifest_ref or None,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"交接单编号已存在: {transfer_id}") from exc
            self.connection.execute(
                "UPDATE batches SET status='in_transit',current_transfer_id=?,updated_at=? WHERE batch_id=?",
                (transfer_id, now, batch_id),
            )
            self._audit("batch", batch_id, "transfer.dispatched", actor["user_id"], {
                "transfer_id": transfer_id,
                "from_party": from_party,
                "to_party": to_party,
                "basis_decision_id": basis["decision_id"],
            })
        return self.get_transfer(transfer_id)

    def receive_transfer(self, actor_id: str, transfer_id: str, note: str = "") -> dict[str, Any]:
        actor = self._require(actor_id, "transfer.receive")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"交接单不存在: {transfer_id}")
            if row["status"] != "dispatched":
                raise InvalidState("交接单已完成接收，不能重复接收")
            now = self._now()
            self.connection.execute(
                "UPDATE transfers SET status='received',received_by=?,received_at=?,receive_note=? "
                "WHERE transfer_id=? AND status='dispatched'",
                (actor["user_id"], now, note or None, transfer_id),
            )
            # 只恢复到发运前固化的状态；若在途期间自身或上游声明被更正、放行依据失效，
            # 则回落到隔离，重复导入或重复接收都无法把问题材料变成可用。
            restored_status = row["prior_status"]
            basis_invalidated = False
            if restored_status == "released" and self._current_release_basis(row["batch_id"]) is None:
                restored_status = "quarantined"
                basis_invalidated = True
            self.connection.execute(
                "UPDATE batches SET status=?,current_transfer_id=NULL,updated_at=? WHERE batch_id=?",
                (restored_status, now, row["batch_id"]),
            )
            self._audit("batch", row["batch_id"], "transfer.received", actor["user_id"], {
                "transfer_id": transfer_id,
                "restored_status": restored_status,
                "release_basis_invalidated": basis_invalidated,
            })
        return self.get_transfer(transfer_id)

    def get_transfer(self, transfer_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if row is None:
            raise NotFound(f"交接单不存在: {transfer_id}")
        return dict(row)

    def list_transfers(self, actor_id: str, batch_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "trace.read")
        self._batch(batch_id)
        rows = self.connection.execute(
            "SELECT * FROM transfers WHERE batch_id=? ORDER BY dispatched_at", (batch_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------------------------------------------------------------- 不可逆处置

    def dispose_batch(
        self,
        actor_id: str,
        batch_id: str,
        method: str,
        authority_doc_ref: str,
        reason: str,
        witness: str,
    ) -> dict[str, Any]:
        actor = self._require(actor_id, "disposal.write")
        method = self._text(method, "method", 128)
        authority_doc_ref = self._text(authority_doc_ref, "authority_doc_ref", 128)
        reason = self._text(reason, "reason", 1024)
        witness_row = self._user(witness)
        if witness_row["user_id"] == actor["user_id"]:
            raise Forbidden("处置执行人与见证人不能为同一人")
        with transaction(self.connection, immediate=True):
            batch = self._batch(batch_id)
            if batch["status"] == "disposed":
                raise InvalidState("批次已完成不可逆处置")
            if batch["status"] == "in_transit":
                raise InvalidState("批次在途，不能处置")
            now = self._now()
            try:
                cursor = self.connection.execute(
                    "INSERT INTO disposals(batch_id,method,authority_doc_ref,reason,witness,disposed_by,disposed_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (batch_id, method, authority_doc_ref, reason, witness_row["user_id"], actor["user_id"], now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该批次已有处置记录") from exc
            self.connection.execute(
                "UPDATE batches SET status='disposed',status_reason=?,updated_at=? WHERE batch_id=?",
                (f"不可逆处置: {method}", now, batch_id),
            )
            self._audit("batch", batch_id, "batch.disposed", actor["user_id"], {
                "disposal_id": cursor.lastrowid,
                "method": method,
                "authority_doc_ref": authority_doc_ref,
                "witness": witness_row["user_id"],
            })
        return dict(self.connection.execute("SELECT * FROM disposals WHERE batch_id=?", (batch_id,)).fetchone())

    # ---------------------------------------------------------------- 追溯与影响

    def trace_batch(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """按批次汇总每次声明、检测、决定、交接、处置与谱系关系。"""

        self._require(actor_id, "trace.read")
        batch = self._batch(batch_id)
        parents = [dict(r) for r in self.connection.execute(
            "SELECT i.batch_id AS parent_batch_id,t.transform_id,t.kind AS transform_kind,"
            "t.created_at,i.quantity,i.unit "
            "FROM transform_inputs i JOIN material_transforms t ON t.transform_id=i.transform_id "
            "JOIN transform_outputs o ON o.transform_id=t.transform_id "
            "WHERE o.batch_id=? ORDER BY t.created_at,i.batch_id",
            (batch_id,),
        ).fetchall()]
        children = [dict(r) for r in self.connection.execute(
            "SELECT o.batch_id,t.transform_id,t.kind AS transform_kind,t.created_at,o.quantity,o.unit "
            "FROM transform_outputs o JOIN material_transforms t ON t.transform_id=o.transform_id "
            "WHERE t.transform_id IN (SELECT transform_id FROM transform_inputs WHERE batch_id=?) "
            "AND o.batch_id<>? ORDER BY t.created_at,o.ordinal",
            (batch_id, batch_id),
        ).fetchall()]
        events = [dict(r) | {"payload": json.loads(r["payload_json"])} for r in self.connection.execute(
            "SELECT event_id,event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='batch' AND entity_id=? ORDER BY event_id",
            (batch_id,),
        ).fetchall()]
        disposal = self.connection.execute(
            "SELECT * FROM disposals WHERE batch_id=?", (batch_id,)
        ).fetchone()
        declarations = self.get_declaration_history(actor_id, batch_id)
        inspections = [dict(r) for r in self.connection.execute(
            "SELECT test_id,sequence_no,test_type,lab,verdict,basis_declaration_version,tested_by,recorded_at "
            "FROM inspections WHERE batch_id=? ORDER BY sequence_no",
            (batch_id,),
        ).fetchall()]
        decisions = [dict(r) for r in self.connection.execute(
            "SELECT decision_id,sequence_no,decision,reason,basis_test_id,basis_declaration_version,"
            "decided_by,decided_at FROM release_decisions WHERE batch_id=? ORDER BY sequence_no",
            (batch_id,),
        ).fetchall()]
        transfers = [dict(r) for r in self.connection.execute(
            "SELECT transfer_id,from_party,to_party,from_location,to_location,status,prior_status,"
            "dispatched_at,received_at FROM transfers WHERE batch_id=? ORDER BY dispatched_at",
            (batch_id,),
        ).fetchall()]
        return {
            "batch": dict(batch),
            "parents": parents,
            "children": children,
            "declarations": declarations,
            "inspections": inspections,
            "decisions": decisions,
            "transfers": transfers,
            "disposal": None if disposal is None else dict(disposal),
            "events": events,
        }

    def impact_analysis(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """父批次信息更正后，向下遍历谱系，标注受影响派生批次与已不可逆处置者。

        对每条派生边，找到该作业发生之后父批次的首次更正时刻；受影响时刻
        沿谱系向下传播。派生批次只有在受影响时刻之后完成复检并据此重新放行，
        才标记为已重新验证。
        """

        self._require(actor_id, "trace.read")
        root = self._batch(batch_id)
        latest_declaration = self._latest_declaration(batch_id)
        corrected_at = latest_declaration["declared_at"] if latest_declaration["version"] > 1 else None
        descendants: list[dict[str, Any]] = []
        # 构建谱系 DAG（合批产物有多个父批次）。
        edges = self.connection.execute(
            "SELECT i.batch_id AS parent_id,o.batch_id AS child_id,t.transform_id "
            "FROM transform_inputs i "
            "JOIN material_transforms t ON t.transform_id=i.transform_id "
            "JOIN transform_outputs o ON o.transform_id=t.transform_id"
        ).fetchall()
        parents_of: dict[str, list[str]] = {}
        children_of: dict[str, list[str]] = {}
        all_nodes = {batch_id}
        for edge in edges:
            parents_of.setdefault(edge["child_id"], []).append(edge["parent_id"])
            children_of.setdefault(edge["parent_id"], []).append(edge["child_id"])
            all_nodes.add(edge["child_id"])
        # 各批次自身更正（version>1）的最早时刻。
        correction_rows = self.connection.execute(
            "SELECT batch_id,min(declared_at) AS first_at FROM source_declarations "
            "WHERE version>1 GROUP BY batch_id"
        ).fetchall()
        first_correction = {row["batch_id"]: row["first_at"] for row in correction_rows}

        def ancestors_of(node: str) -> set[str]:
            result: set[str] = set()
            stack = list(parents_of.get(node, ()))
            while stack:
                current = stack.pop()
                if current in result:
                    continue
                result.add(current)
                stack.extend(parents_of.get(current, ()))
            return result

        # 谱系深度（取最短路径）。
        depths = {batch_id: 0}
        queue_depths = [batch_id]
        while queue_depths:
            current = queue_depths.pop(0)
            for child_id in children_of.get(current, ()):
                candidate = depths[current] + 1
                if child_id not in depths or candidate < depths[child_id]:
                    depths[child_id] = candidate
                    queue_depths.append(child_id)
        via_transform: dict[str, str] = {}
        for edge in edges:
            via_transform.setdefault(edge["child_id"], edge["transform_id"])
        ordered = sorted(
            (node for node in all_nodes if node != batch_id),
            key=lambda node: (depths.get(node, 999), node),
        )
        for child_id in ordered:
            child = self._batch(child_id)
            # 权威判据：子批次创建之后，任何祖先发生过声明更正，即受影响。
            ancestor_corrections = [
                first_correction[ancestor_id]
                for ancestor_id in ancestors_of(child_id)
                if ancestor_id in first_correction
                and first_correction[ancestor_id] > child["created_at"]
            ]
            since = min(ancestor_corrections) if ancestor_corrections else None
            latest_decision = self.connection.execute(
                "SELECT d.decided_at,d.decision,d.basis_test_id,d.basis_declaration_version,"
                "i.recorded_at AS test_recorded_at "
                "FROM release_decisions d LEFT JOIN inspections i ON i.test_id=d.basis_test_id "
                "WHERE d.batch_id=? ORDER BY d.sequence_no DESC LIMIT 1",
                (child_id,),
            ).fetchone()
            revalidated = False
            if since is not None and latest_decision is not None:
                revalidated = (
                    latest_decision["decision"] == "release"
                    and latest_decision["decided_at"] > since
                    and latest_decision["test_recorded_at"] is not None
                    and latest_decision["test_recorded_at"] > since
                )
            descendants.append({
                "batch_id": child_id,
                "via_transform": via_transform.get(child_id),
                "depth": depths.get(child_id),
                "status": child["status"],
                "affected_by_correction": since is not None,
                "affected_since": since,
                "revalidated_after_correction": revalidated,
                "irreversible": child["status"] == "disposed",
                "latest_decision": None if latest_decision is None else {
                    "decision": latest_decision["decision"],
                    "decided_at": latest_decision["decided_at"],
                    "basis_test_id": latest_decision["basis_test_id"],
                },
            })
        return {
            "batch_id": batch_id,
            "current_declaration_version": root["declaration_version"],
            "corrected": latest_declaration["version"] > 1,
            "last_corrected_at": corrected_at,
            "descendants": descendants,
            "affected_count": sum(1 for item in descendants if item["affected_by_correction"]),
            "irreversible_count": sum(1 for item in descendants if item["irreversible"]),
        }
