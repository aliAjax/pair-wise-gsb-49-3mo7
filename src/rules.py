"""再保险合约与巨灾暴露管理领域规则与状态转换。"""
from typing import Any, Dict, Tuple

from .domain import Conflict, ValidationError, number, text

INITIAL_STATE = "quoted"
CREATE_ROLES = {'underwriter'}
# cancel 是 withdraw 的业务别名，统一按撤销处理。
ACTION_ALIASES = {'cancel': 'withdraw'}
ACTION_ROLES = {
    'bind': {'underwriter'},
    'submit_claim': {'claims_officer'},
    'calculate': {'claims_officer'},
    'settle': {'finance'},
    'reject': {'finance', 'claims_officer'},
    'withdraw': {'claims_officer', 'finance'},
}
TRANSITIONS = {
    'bind': {'quoted': 'bound'},
    'submit_claim': {'bound': 'claim_submitted'},
    'calculate': {'claim_submitted': 'calculated'},
    'settle': {'calculated': 'settled'},
    'reject': {'claim_submitted': 'rejected', 'calculated': 'rejected'},
    'withdraw': {'claim_submitted': 'withdrawn', 'calculated': 'withdrawn'},
}


class DomainRules:
    INITIAL_STATE = INITIAL_STATE

    def canonical_action(self, action: str) -> str:
        return ACTION_ALIASES.get(action, action)

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        action = self.canonical_action(action)
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
        # 恢复次数默认1，允许0（合约不允许恢复时只能拒赔/撤销）。
        value = p.get("reinstatements", 1)
        if value is None:
            value = 1
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValidationError("reinstatements必须是非负整数")
        p["reinstatements"] = value
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
        return p

    def layer_capacity(self, payload: Dict[str, Any]) -> float:
        """单次恢复可摊回的层容量：层宽 × 分出比例。由事件首单确定。"""
        return round(float(payload["layer_width"]) * float(payload["cession_pct"]), 2)

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        action = self.canonical_action(action)
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        canonical = self.canonical_action(action)
        new_state = self.require_transition(record, canonical)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if canonical == "bind":
            changes["bound_by"] = text(data, "underwriter_id")
            summary = "再保合约已绑定"
        elif canonical == "submit_claim":
            changes["claim_number"] = text(data, "claim_number")
            claim_event_id = text(data, "event_id")
            if claim_event_id != p.get("event_id"):
                raise ValidationError("赔案事件与合约事件不一致")
            changes["claim_event_id"] = claim_event_id
            summary = "赔案已提交"
        elif canonical == "calculate":
            loss = number(data, "approved_loss", 0)
            width = float(p["layer_width"])
            recovery = min(max(0.0, loss - float(p["attachment"])), width) * float(p["cession_pct"])
            changes["approved_loss"] = loss
            changes["recoverable_amount"] = round(recovery, 2)
            changes["reinstatement_premium"] = round(recovery * float(p["reinstatement_pct"]), 2)
            summary = "摊回金额已核定并占用事件额度"
        elif canonical == "settle":
            if float(p["recoverable_amount"]) <= 0:
                raise ValidationError("无可结算摊回")
            changes["payment_reference"] = text(data, "payment_reference")
            summary = "摊回赔款已结算并消耗一次恢复"
        elif canonical == "reject":
            changes["reject_reason"] = text(data, "reject_reason")
            summary = "赔案已拒绝，占用已释放"
        elif canonical == "withdraw":
            reason = data.get("withdraw_reason", data.get("reason", ""))
            if reason is None:
                reason = ""
            if not isinstance(reason, str):
                raise ValidationError("withdraw_reason必须是文本")
            changes["withdraw_reason"] = reason.strip()
            summary = "赔案已撤销，占用已释放"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
