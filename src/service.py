"""业务用例编排：权限检查、事件暴露台账核对与审计。

台账语义：
- 事件首单确定单层容量（层宽×分出比例）与恢复次数。
- 核定（calculate）先占额度：占用计入事件，不耗次数、不计保费。
- 结算（settle）才消耗一次恢复并累计恢复保费，付款与保费留档。
- 未结算案件拒赔/撤销时释放占用；已结算记录不可释放，付款与保费保留。
所有核对与落账在同一个 BEGIN IMMEDIATE 事务内完成，保证并发串行。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, Conflict, NotFound, PermissionDenied, text
from .repository import Repository
from .rules import DomainRules

TOLERANCE = 0.01


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    @staticmethod
    def _event_id(record: dict) -> str:
        return str(record["payload"]["event_id"])

    def _capacity_conflict(self, gateway, event_id: str, message: str) -> Conflict:
        return Conflict(message, details=gateway.event_details(event_id))

    # ------------------------------------------------------------------
    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        event_id = str(prepared["event_id"])
        capacity = self.rules.layer_capacity(prepared)
        reinstatements = int(prepared["reinstatements"])
        # 建单与首案建账在同一事务：写入失败重开时不会留下半条台账。
        with self.repository.ledger_gateway() as gateway:
            record = gateway.insert_record(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)
            gateway.ensure_event(event_id, capacity, reinstatements, record["id"], actor.user_id)
            gateway.commit()
        return record

    # ------------------------------------------------------------------
    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        canonical = self.rules.canonical_action(action)
        if not self.rules.role_can_action(actor.role, canonical):
            raise PermissionDenied("角色无权执行该操作")

        with self.repository.ledger_gateway() as gateway:
            record = gateway.get_record(record_id)
            event_id = self._event_id(record)
            # 兼容旧数据：回填缺失的台账关联，容量沿用本单（首案）参数。
            gateway.ensure_event(
                event_id,
                self.rules.layer_capacity(record["payload"]),
                int(record["payload"].get("reinstatements", 1)),
                record["id"],
                actor.user_id,
            )
            new_state, new_payload, summary = self.rules.apply_action(record, canonical, data or {})

            if canonical == "calculate":
                self._reserve(gateway, record, new_payload, event_id, actor.user_id)
            elif canonical == "settle":
                self._consume(gateway, record, new_payload, event_id, actor.user_id)
            elif canonical in ("reject", "withdraw"):
                self._release(gateway, record, event_id)

            details = {
                "summary": summary,
                "input": data or {},
                "from": record["state"],
                "to": new_state,
                "ledger": gateway.event_details(event_id),
            }
            result = gateway.update_record(
                record_id=record_id,
                expected_version=int(expected_version),
                state=new_state,
                payload=new_payload,
                actor_id=actor.user_id,
                action=canonical,
                details=details,
            )
            gateway.commit()
        return result

    # ------------------------------------------------------------------
    # 台账动作
    # ------------------------------------------------------------------
    def _reserve(self, gateway, record: Dict[str, Any], payload: Dict[str, Any], event_id: str, actor_id: str) -> None:
        """核定先占额度。已有占用时按同一事件同一赔案核对差额（重开幂等）。"""
        amount = round(float(payload["recoverable_amount"]), 2)
        premium = round(float(payload["reinstatement_premium"]), 2)
        event = gateway.get_event(event_id)
        occupancy = gateway.get_occupancy(event_id, record["id"])
        if occupancy and occupancy["state"] == "settled":
            # 已结算赔案的占用为终态，重开核对时不再叠加占用。
            return
        held = round(float(occupancy["amount"]), 2) if occupancy and occupancy["state"] == "reserved" else 0.0
        occupied = round(float(event["occupied_amount"]), 2)
        projected = round(occupied - held + amount, 2)
        if projected > float(event["capacity"]) + TOLERANCE:
            raise self._capacity_conflict(gateway, event_id, "同事件占用超过事件容量，无法核定")
        gateway.adjust_event(event_id, occupied_delta=round(amount - held, 2))
        gateway.upsert_occupancy(
            event_id, record["id"], "reserved", amount, premium, actor_id,
            claim_number=payload.get("claim_number", ""),
        )

    def _consume(self, gateway, record: Dict[str, Any], payload: Dict[str, Any], event_id: str, actor_id: str) -> None:
        """结算才耗次数并累计保费，付款与保费在占用明细上留档。

        重开（同一赔案重复落账）按事件+赔案核对：已结算且付款一致则幂等通过；
        占用金额变动时在容量内调整，不重复消耗次数。
        """
        amount = round(float(payload["recoverable_amount"]), 2)
        premium = round(float(payload["reinstatement_premium"]), 2)
        payment_reference = payload.get("payment_reference", "")
        claim_number = payload.get("claim_number", "")
        event = gateway.get_event(event_id)
        occupancy = gateway.get_occupancy(event_id, record["id"])
        used = int(event["reinstatements_used"])
        remaining = int(event["reinstatements"]) - used
        occupied = round(float(event["occupied_amount"]), 2)

        if occupancy and occupancy["state"] == "settled":
            # 写入失败后重开：同一事件同一赔案恢复占用核对，不重复耗次数。
            prior_amount = round(float(occupancy["amount"]), 2)
            prior_premium = round(float(occupancy["premium"]), 2)
            gateway.adjust_event(
                event_id,
                occupied_delta=round(amount - prior_amount, 2),
                settled_delta=round(amount - prior_amount, 2),
                premium_delta=round(premium - prior_premium, 2),
                actor_id=actor_id,
            )
            gateway.upsert_occupancy(
                event_id, record["id"], "settled", amount, premium, actor_id,
                claim_number=claim_number, payment_reference=payment_reference,
            )
            return

        held = round(float(occupancy["amount"]), 2) if occupancy and occupancy["state"] == "reserved" else 0.0
        if remaining <= 0:
            raise self._capacity_conflict(gateway, event_id, "事件恢复次数已用尽，结算被拒绝")
        # 结算前必须有核定占用；旧数据回填缺失时现场补占并复核容量。
        if occupied - held + amount > float(event["capacity"]) + TOLERANCE:
            raise self._capacity_conflict(gateway, event_id, "同事件占用超过事件容量，无法结算")
        gateway.adjust_event(
            event_id,
            occupied_delta=round(amount - held, 2),
            settled_delta=amount,
            used_delta=1,
            premium_delta=premium,
            actor_id=actor_id,
        )
        gateway.upsert_occupancy(
            event_id, record["id"], "settled", amount, premium, actor_id,
            claim_number=claim_number, payment_reference=payment_reference,
        )

    def _release(self, gateway, record: Dict[str, Any], event_id: str) -> None:
        """拒赔/撤销：仅释放未结算（reserved）占用；已结算付款与保费留档。"""
        occupancy = gateway.get_occupancy(event_id, record["id"])
        if occupancy is None or occupancy["state"] != "reserved":
            return
        amount = round(float(occupancy["amount"]), 2)
        gateway.adjust_event(event_id, occupied_delta=-amount)
        gateway.upsert_occupancy(
            event_id, record["id"], "released", amount, round(float(occupancy["premium"]), 2), "",
            claim_number=occupancy.get("claim_number", ""),
        )

    # ------------------------------------------------------------------
    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    def list_events(self, actor: Actor, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_events(limit=limit)

    def get_event(self, actor: Actor, event_id: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        details = self.repository.event_details(event_id)
        if details is None:
            raise NotFound("事件台账不存在")
        return details

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
