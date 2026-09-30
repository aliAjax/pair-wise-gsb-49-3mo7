import json
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


def make_data(event_id="CAT-2026-01", reinstatements=1):
    return {'event_id': event_id, 'attachment': 1000000.0, 'limit': 5000000.0,
            'cession_pct': 0.4, 'loss_amount': 3000000.0, 'reinstatement_pct': 0.15,
            'aggregate_prior': 0.0, 'reinstatements': reinstatements}


def drive(service, record, steps):
    """steps: [(role, action, data), ...]"""
    for role, action, data in steps:
        record = service.act(Actor("op", role), record["id"], record["version"], action, data)
    return record


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "test.db")
        self.service = build_service(self.db)

    def tearDown(self):
        self.temp.cleanup()

    def _create_case(self, ref, claim, event_id="CAT-2026-01", reinstatements=1):
        record = self.service.create(Actor("uw", "underwriter"), ref, make_data(event_id, reinstatements))
        record = drive(self.service, record, [
            ("underwriter", "bind", {"underwriter_id": "UW-1"}),
            ("claims_officer", "submit_claim", {"claim_number": claim, "event_id": event_id}),
        ])
        return record

    def test_first_case_sets_capacity_and_reinstatements(self):
        self._create_case("RI-1", "CLM-1")
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["capacity"], 1600000.0)       # (500w-100w)*0.4
        self.assertEqual(event["reinstatements"], 1)
        self.assertEqual(event["reinstatements_remaining"], 1)

    def test_calculate_reserves_settle_consumes(self):
        record = self._create_case("RI-1", "CLM-1")
        record = drive(self.service, record, [
            ("claims_officer", "calculate", {"approved_loss": 2800000.0}),
        ])
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        # 核定先占额度：不耗次数、不计保费
        self.assertEqual(event["occupied_amount"], 720000.0)
        self.assertEqual(event["settled_amount"], 0.0)
        self.assertEqual(event["reinstatements_used"], 0)
        self.assertEqual(event["premium_received"], 0.0)

        record = drive(self.service, record, [
            ("finance", "settle", {"payment_reference": "PAY-1"}),
        ])
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["occupied_amount"], 720000.0)
        self.assertEqual(event["settled_amount"], 720000.0)
        self.assertEqual(event["reinstatements_used"], 1)
        self.assertEqual(event["reinstatements_remaining"], 0)
        self.assertEqual(event["premium_received"], 108000.0)  # 72w*15%
        # 付款与保费留档
        occ = [o for o in event["occupancies"] if o["claim_number"] == "CLM-1"][0]
        self.assertEqual(occ["state"], "settled")
        self.assertEqual(occ["payment_reference"], "PAY-1")
        self.assertEqual(occ["premium"], 108000.0)
        self.assertEqual(record["state"], "settled")

    def test_calculate_beyond_capacity_rejected_with_details(self):
        first = self._create_case("RI-1", "CLM-1")
        drive(self.service, first, [("claims_officer", "calculate", {"approved_loss": 5000000.0})])
        # 第一案占满160w容量；第二案核定应失败
        second = self._create_case("RI-2", "CLM-2")
        with self.assertRaises(Conflict) as cm:
            drive(self.service, second, [("claims_officer", "calculate", {"approved_loss": 5000000.0})])
        details = cm.exception.details
        self.assertEqual(details["available_amount"], 0.0)
        self.assertEqual(details["reinstatements_remaining"], 1)  # 核定不耗次数
        self.assertEqual(len(details["occupancies"]), 1)
        self.assertEqual(details["occupancies"][0]["claim_number"], "CLM-1")

    def test_reject_and_withdraw_release_reservation(self):
        # 核定后拒赔释放
        rec1 = self._create_case("RI-1", "CLM-1")
        drive(self.service, rec1, [
            ("claims_officer", "calculate", {"approved_loss": 2800000.0}),
            ("claims_officer", "reject", {"reject_reason": "材料不足"}),
        ])
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["occupied_amount"], 0.0)
        self.assertEqual(event["reinstatements_used"], 0)
        occ = event["occupancies"][0]
        self.assertEqual(occ["state"], "released")

        # 第二案核定后撤销（cancel别名）同样释放
        rec2 = self._create_case("RI-2", "CLM-2")
        drive(self.service, rec2, [
            ("claims_officer", "calculate", {"approved_loss": 2800000.0}),
            ("claims_officer", "cancel", {"withdraw_reason": "客户撤案"}),
        ])
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["occupied_amount"], 0.0)
        occ2 = [o for o in event["occupancies"] if o["claim_number"] == "CLM-2"][0]
        self.assertEqual(occ2["state"], "released")

        # 释放后容量与次数恢复，第三案可完整结算
        rec3 = self._create_case("RI-3", "CLM-3")
        drive(self.service, rec3, [
            ("claims_officer", "calculate", {"approved_loss": 2800000.0}),
            ("finance", "settle", {"payment_reference": "PAY-3"}),
        ])
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["reinstatements_used"], 1)
        self.assertEqual(event["premium_received"], 108000.0)

    def test_concurrent_settle_only_one_wins_last_reinstatement(self):
        rec1 = self._create_case("RI-1", "CLM-1")
        rec2 = self._create_case("RI-2", "CLM-2")
        drive(self.service, rec1, [("claims_officer", "calculate", {"approved_loss": 2800000.0})])
        rec2 = drive(self.service, rec2, [("claims_officer", "calculate", {"approved_loss": 2800000.0})])
        rec1 = self.service.get_record(Actor("uw", "underwriter"), rec1["id"])

        barrier = threading.Barrier(2)
        outcomes = []

        def settle(record, payref):
            try:
                barrier.wait()
                result = self.service.act(Actor("fin", "finance"), record["id"], record["version"],
                                          "settle", {"payment_reference": payref})
                outcomes.append(("ok", result["id"]))
            except Conflict as exc:
                outcomes.append(("conflict", exc.details))

        t1 = threading.Thread(target=settle, args=(rec1, "PAY-1"))
        t2 = threading.Thread(target=settle, args=(rec2, "PAY-2"))
        t1.start(); t2.start(); t1.join(); t2.join()

        results = [o[0] for o in outcomes]
        self.assertEqual(sorted(results), ["conflict", "ok"])
        loser = next(o[1] for o in outcomes if o[0] == "conflict")
        self.assertEqual(loser["reinstatements_remaining"], 0)
        self.assertEqual(loser["reinstatements_used"], 1)
        self.assertEqual(loser["premium_received"], 108000.0)
        states = sorted(o["state"] for o in loser["occupancies"])
        self.assertEqual(states, ["reserved", "settled"])
        # 败方赔案仍是 calculated，占用保留；撤销后释放
        loser_record = self.service.get_record(Actor("uw", "underwriter"), next(
            o["record_id"] for o in loser["occupancies"] if o["state"] == "reserved"))
        self.assertEqual(loser_record["state"], "calculated")
        drive(self.service, loser_record, [("claims_officer", "withdraw", {"withdraw_reason": "无剩余次数"})])
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["occupied_amount"], 720000.0)  # 只剩赢家占用

    def test_reopen_reconciles_same_event_occupancy(self):
        record = self._create_case("RI-1", "CLM-1")
        record = drive(self.service, record, [
            ("claims_officer", "calculate", {"approved_loss": 2800000.0}),
            ("finance", "settle", {"payment_reference": "PAY-1"}),
        ])
        repo = self.service.repository
        # 模拟写入失败后重开：对同一赔案重复落账，必须按(event,record)核对而非重复计数
        with repo.ledger_gateway() as gw:
            fresh = gw.get_record(record["id"])
            self.service._reserve(gw, fresh, fresh["payload"], "CAT-2026-01", "retry")
            gw.commit()
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["occupied_amount"], 720000.0)
        self.assertEqual(event["reinstatements_used"], 1)
        self.assertEqual(event["premium_received"], 108000.0)

        with repo.ledger_gateway() as gw:
            fresh = gw.get_record(record["id"])
            self.service._consume(gw, fresh, fresh["payload"], "CAT-2026-01", "retry")
            gw.commit()
        event = self.service.get_event(Actor("uw", "underwriter"), "CAT-2026-01")
        self.assertEqual(event["reinstatements_used"], 1)       # 不重复耗次数
        self.assertEqual(event["settled_amount"], 720000.0)
        self.assertEqual(event["premium_received"], 108000.0)

    def test_zero_reinstatements_blocks_settle(self):
        record = self._create_case("RI-1", "CLM-1", reinstatements=0)
        record = drive(self.service, record, [
            ("claims_officer", "calculate", {"approved_loss": 2800000.0}),
        ])
        with self.assertRaises(Conflict) as cm:
            drive(self.service, record, [("finance", "settle", {"payment_reference": "PAY-X"})])
        self.assertEqual(cm.exception.details["reinstatements_remaining"], 0)

    def test_legacy_backfill_keeps_original_states(self):
        # 用底层连接写入没有事件关联的旧数据
        repo = self.service.repository
        now = "2026-01-01T00:00:00+00:00"
        legacy = [
            ("RI-OLD-1", "calculated", 720000.0, 108000.0, "CLM-A", ""),
            ("RI-OLD-2", "settled", 400000.0, 60000.0, "CLM-B", "PAY-OLD"),
        ]
        with repo._connect() as conn:
            conn.execute("DELETE FROM events")
            conn.execute("DELETE FROM occupancies")
            for ref, state, recovery, premium, claim, payref in legacy:
                payload = dict(make_data("CAT-LEGACY"))
                payload.pop("reinstatements")  # 旧数据没有恢复次数字段
                payload.update({"layer_width": 4000000.0, "recoverable_amount": recovery,
                                "reinstatement_premium": premium, "claim_number": claim,
                                "payment_reference": payref})
                conn.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (ref, state, 3 if state == "settled" else 2,
                     json.dumps(payload, ensure_ascii=False), "legacy", "legacy", now, now))

        # 重开仓库触发回填
        rebuilt = build_service(self.db)
        event = rebuilt.get_event(Actor("uw", "underwriter"), "CAT-LEGACY")
        self.assertEqual(event["capacity"], 1600000.0)
        self.assertEqual(event["reinstatements"], 1)          # 旧数据缺省1次
        self.assertEqual(event["occupied_amount"], 1120000.0)
        self.assertEqual(event["settled_amount"], 400000.0)
        self.assertEqual(event["reinstatements_used"], 1)
        self.assertEqual(event["premium_received"], 60000.0)
        by_claim = {o["claim_number"]: o for o in event["occupancies"]}
        self.assertEqual(by_claim["CLM-A"]["state"], "reserved")
        self.assertEqual(by_claim["CLM-B"]["state"], "settled")
        self.assertEqual(by_claim["CLM-B"]["payment_reference"], "PAY-OLD")

        # 回填后按原状态继续：calculated 的案件可继续撤销释放；settled 终态不可再操作
        rec_a = [r for r in rebuilt.list_records(Actor("uw", "underwriter")) if r["reference"] == "RI-OLD-1"][0]
        self.assertEqual(rec_a["state"], "calculated")
        drive(rebuilt, rec_a, [("claims_officer", "withdraw", {"withdraw_reason": "回填后续处理"})])
        event = rebuilt.get_event(Actor("uw", "underwriter"), "CAT-LEGACY")
        self.assertEqual(event["occupied_amount"], 400000.0)  # 只剩已结算占用
        self.assertEqual(event["reinstatements_used"], 1)    # 已结算次数与保费留档
        self.assertEqual(event["premium_received"], 60000.0)


if __name__ == "__main__":
    unittest.main()
