"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from typing import Any, Dict, Iterable, Tuple

from .domain import Conflict, ValidationError, integer, number, text


INITIAL_STATE = "quoted"
DEFAULT_REINSTATEMENT_COUNT = 1
# 仍占用事件容量的案件状态：计算中（预先占用）与已结算（已消耗）。
CAPACITY_STATES = {"calculated", "settled"}
CREATE_ROLES = {'underwriter'}
ACTION_ROLES = {
    'bind': {'underwriter'},
    'submit_claim': {'claims_officer'},
    'calculate': {'claims_officer'},
    'settle': {'finance'},
    'reject': {'finance', 'claims_officer'},
    'revoke': {'finance', 'claims_officer'},
}
TRANSITIONS = {
    'bind': {'quoted': 'bound'},
    'submit_claim': {'bound': 'claim_submitted'},
    'calculate': {'claim_submitted': 'calculated'},
    'settle': {'calculated': 'settled'},
    'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'},
    'revoke': {'claim_submitted': 'cancelled', 'calculated': 'cancelled'},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    CAPACITY_STATES = CAPACITY_STATES

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "event_id")
        attachment = number(p, "attachment", 0)
        limit = number(p, "limit", 0)
        number(p, "cession_pct", 0, 1)
        number(p, "loss_amount", 0)
        number(p, "reinstatement_pct", 0, 1)
        number(p, "aggregate_prior", 0)
        if "reinstatement_count" in p:
            integer(p, "reinstatement_count", 0)
        if limit <= attachment:
            raise ValidationError("赔款限额必须高于起赔点")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        width = float(p["limit"]) - float(p["attachment"])
        retained_loss = max(0.0, float(p["loss_amount"]) - float(p["attachment"]))
        recovery = min(retained_loss, width) * float(p["cession_pct"])
        p["layer_width"] = round(width, 2)
        p["recoverable_amount"] = round(recovery, 2)
        p["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
        p["net_retention"] = round(float(p["loss_amount"]) - recovery, 2)
        # 事件容量内可用恢复次数；首案提交时写入事件台账。旧数据缺省按1次处理。
        if "reinstatement_count" not in p or p.get("reinstatement_count") is None:
            p["reinstatement_count"] = DEFAULT_REINSTATEMENT_COUNT
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        """创建/绑定时的轻量预检，权威扣减在calculate事务内完成。

        只有已核定（calculated，预先占用）和已结算（settled，已消耗）的
        案件仍占用事件容量；拒赔与撤销的案件已释放，不再计入。
        """
        event_id = payload.get("event_id")
        # 预检只提示该事件的已占/已耗额度是否已打满；本笔实际占用以核定时事务内扣减为准。
        used = float(payload.get("aggregate_prior", 0))
        for item in existing:
            if item["state"] not in CAPACITY_STATES or item["payload"].get("event_id") != event_id:
                continue
            used += float(item["payload"].get("recoverable_amount", 0))
        capacity = float(payload["layer_width"]) * float(payload["cession_pct"])
        if used > capacity + 0.01:
            raise Conflict("同一事件累计摊回已超过再保容量")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        """纯字段层面的动作处理；事件容量/恢复次数的扣减与释放在service/repository事务内完成。"""
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            summary = "再保合约已绑定"
        elif action == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            changes["claim_event_id"] = text(data, "event_id") if data.get("event_id") else p.get("event_id")
            summary = "赔案已提交"
        elif action == "calculate":
            loss = number(data, "approved_loss", 0)
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            summary = "摊回金额已计算，已预先占用事件额度与恢复次数"
        elif action == "settle":
            recovery = float(p.get("recoverable_amount", 0))
            premium = float(p.get("reinstatement_premium", 0))
            if recovery <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            # 付款与保费在结算时落档，事件台账据此累计已收保费。
            changes["settled_recovery"] = round(recovery, 2)
            changes["settled_premium"] = round(premium, 2)
            summary = "摊回赔款已结算，恢复次数已消耗并确认恢复保费"
        elif action == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒赔，预先占用的事件额度与恢复次数已释放"
        elif action == "revoke":
            changes["revoke_reason"] = text(data, "revoke_reason")
            summary = "赔案已撤销，预先占用的事件额度与恢复次数已释放"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
