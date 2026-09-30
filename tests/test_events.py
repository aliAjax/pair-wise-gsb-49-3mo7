"""事件暴露台账与赔案流转集成测试。

覆盖：首案确定事件容量与恢复次数、核定预占、结算消耗次数并累计保费、
拒赔/撤销释放、并发争抢最后一次恢复、写入失败重开核对与旧数据回填。
"""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict


UW = Actor("uw", "underwriter")
CO = Actor("co", "claims_officer")
FN = Actor("fn", "finance")
ADMIN = Actor("admin", "admin")


def make_create_data(event_id, reinstatement_count=1, **over):
    data = {
        'event_id': event_id,
        'attachment': 1000000.0,
        'limit': 5000000.0,
        'cession_pct': 0.4,
        'loss_amount': 3000000.0,
        'reinstatement_pct': 0.15,
        'aggregate_prior': 0.0,
        'reinstatement_count': reinstatement_count,
    }
    data.update(over)
    return data


def bind_and_submit(service, reference, event_id, reinstatement_count=1, claim_number=None):
    record = service.create(UW, reference, make_create_data(event_id, reinstatement_count))
    record = service.act(UW, record["id"], record["version"], "bind", {"underwriter_id": "UW-1"})
    record = service.act(
        CO, record["id"], record["version"], "submit_claim",
        {"claim_number": claim_number or reference + "-CLM", "event_id": event_id},
    )
    return record


class EventLedgerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "events.db")
        self.service = build_service(self.db_path)

    def tearDown(self):
        self.temp.cleanup()

    def test_first_claim_anchors_event_capacity_and_reinstatements(self):
        record = bind_and_submit(self.service, "RI-1", "CAT-ANCHOR", reinstatement_count=2)
        event = self.service.get_event(ADMIN, "CAT-ANCHOR")
        self.assertEqual(event["layer_capacity"], 1600000.0)
        self.assertEqual(event["total_reinstatements"], 2)
        self.assertEqual(event["anchor_record_id"], record["id"])
        self.assertEqual(event["used_reinstatements"], 0)
        self.assertEqual(event["reserved_reinstatements"], 0)
        self.assertEqual(event["remaining_reinstatements"], 2)

    def test_calculate_reserves_and_settle_consumes_and_accumulates_premium(self):
        record = bind_and_submit(self.service, "RI-1", "CAT-FLOW", reinstatement_count=2)
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        event = self.service.get_event(ADMIN, "CAT-FLOW")
        # (280w-100w)*0.4 = 72w，核定只预占不耗次数、不计保费
        self.assertEqual(event["reserved_amount"], 720000.0)
        self.assertEqual(event["reserved_reinstatements"], 1)
        self.assertEqual(event["remaining_reinstatements"], 1)
        self.assertEqual(event["used_reinstatements"], 0)
        self.assertEqual(event["received_premium"], 0.0)

        record = self.service.act(FN, record["id"], record["version"], "settle", {"payment_reference": "PAY-1"})
        event = self.service.get_event(ADMIN, "CAT-FLOW")
        self.assertEqual(event["used_reinstatements"], 1)
        self.assertEqual(event["reserved_reinstatements"], 0)
        self.assertEqual(event["settled_amount"], 720000.0)
        self.assertEqual(event["received_premium"], 108000.0)
        # 付款与保费在案件与占用明细上均留档
        self.assertEqual(record["payload"]["settled_recovery"], 720000.0)
        self.assertEqual(record["payload"]["settled_premium"], 108000.0)
        consumed = [o for o in event["occupancies"] if o["status"] == "consumed"]
        self.assertEqual(len(consumed), 1)
        self.assertEqual(consumed[0]["payment_reference"], "PAY-1")
        self.assertEqual(consumed[0]["settled_amount"], 720000.0)
        self.assertEqual(consumed[0]["reinstatement_premium"], 108000.0)

    def test_reject_before_settlement_releases_occupancy(self):
        record = bind_and_submit(self.service, "RI-1", "CAT-REJ", reinstatement_count=1)
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        event = self.service.get_event(ADMIN, "CAT-REJ")
        self.assertEqual(event["remaining_reinstatements"], 0)

        record = self.service.act(
            CO, record["id"], record["version"], "reject", {"reject_reason": "不属于保障范围"},
        )
        self.assertEqual(record["state"], "rejected")
        event = self.service.get_event(ADMIN, "CAT-REJ")
        self.assertEqual(event["reserved_reinstatements"], 0)
        self.assertEqual(event["remaining_reinstatements"], 1)
        self.assertEqual(event["reserved_amount"], 0.0)
        self.assertEqual(event["used_reinstatements"], 0)
        self.assertTrue(all(o["status"] == "released" for o in event["occupancies"]))

        # 额度归还后同事件另一笔案件可以核定并结算
        other = bind_and_submit(self.service, "RI-2", "CAT-REJ", reinstatement_count=1)
        other = self.service.act(CO, other["id"], other["version"], "calculate", {"approved_loss": 2000000.0})
        other = self.service.act(FN, other["id"], other["version"], "settle", {"payment_reference": "PAY-2"})
        event = self.service.get_event(ADMIN, "CAT-REJ")
        self.assertEqual(event["used_reinstatements"], 1)
        self.assertEqual(event["settled_amount"], 400000.0)

    def test_revoke_calculated_claim_releases_occupancy(self):
        record = bind_and_submit(self.service, "RI-1", "CAT-REV", reinstatement_count=1)
        record = self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        record = self.service.act(CO, record["id"], record["version"], "revoke", {"revoke_reason": "重复报案"})
        self.assertEqual(record["state"], "cancelled")
        event = self.service.get_event(ADMIN, "CAT-REV")
        self.assertEqual(event["remaining_reinstatements"], 1)
        self.assertEqual(event["reserved_amount"], 0.0)
        self.assertTrue(all(o["status"] == "released" for o in event["occupancies"]))

    def test_concurrent_last_reinstatement_only_one_succeeds(self):
        r1 = bind_and_submit(self.service, "RI-A", "CAT-RACE", reinstatement_count=1)
        r2 = bind_and_submit(self.service, "RI-B", "CAT-RACE", reinstatement_count=1)
        outcomes = {}
        barrier = threading.Barrier(2)

        def worker(key, record):
            barrier.wait()
            try:
                self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 3000000.0})
                outcomes[key] = "ok"
            except Conflict as exc:
                outcomes[key] = exc.details

        t1 = threading.Thread(target=worker, args=("a", r1))
        t2 = threading.Thread(target=worker, args=("b", r2))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        winners = [k for k, v in outcomes.items() if v == "ok"]
        losers = [v for k, v in outcomes.items() if v != "ok"]
        self.assertEqual(winners, ["a"] if "a" in winners else ["b"])
        self.assertEqual(len(losers), 1)
        details = losers[0]
        self.assertEqual(details["reason"], "no_reinstatement_left")
        self.assertEqual(details["remaining_reinstatements"], 0)
        self.assertEqual(details["total_reinstatements"], 1)
        # 失败方拿到另一方的占用明细
        self.assertEqual(len(details["occupancies"]), 1)
        self.assertEqual(details["occupancies"][0]["status"], "reserved")
        self.assertEqual(details["occupancies"][0]["reserved_amount"], 800000.0)

        event = self.service.get_event(ADMIN, "CAT-RACE")
        self.assertEqual(event["reserved_reinstatements"], 1)
        self.assertEqual(event["remaining_reinstatements"], 0)

    def test_capacity_conflict_reports_remaining(self):
        r1 = bind_and_submit(self.service, "RI-1", "CAT-CAP", reinstatement_count=3)
        r1 = self.service.act(CO, r1["id"], r1["version"], "calculate", {"approved_loss": 3500000.0})
        r2 = bind_and_submit(self.service, "RI-2", "CAT-CAP", reinstatement_count=3)
        with self.assertRaises(Conflict) as cm:
            self.service.act(CO, r2["id"], r2["version"], "calculate", {"approved_loss": 3000000.0})
        details = cm.exception.details
        self.assertEqual(details["reason"], "insufficient_capacity")
        self.assertEqual(details["available_capacity"], 600000.0)
        self.assertEqual(details["remaining_reinstatements"], 2)
        self.assertEqual(details["requested_amount"], 800000.0)

    def test_reopen_reconciles_interrupted_occupancy(self):
        # 案件已核定（占用应存在），重开后台账仍与占用明细一致
        record = bind_and_submit(self.service, "RI-1", "CAT-REOPEN", reinstatement_count=2)
        self.service.act(CO, record["id"], record["version"], "calculate", {"approved_loss": 2800000.0})
        service2 = build_service(self.db_path)
        summary = service2.reconcile_events()
        self.assertEqual(summary["events"], 1)
        event = service2.get_event(ADMIN, "CAT-REOPEN")
        self.assertEqual(event["reserved_reinstatements"], 1)
        self.assertEqual(event["remaining_reinstatements"], 1)
        self.assertEqual(event["reserved_amount"], 720000.0)
        # 重开核对后可按原状态继续结算
        record = service2.get_record(ADMIN, record["id"])
        record = service2.act(FN, record["id"], record["version"], "settle", {"payment_reference": "PAY-X"})
        event = service2.get_event(ADMIN, "CAT-REOPEN")
        self.assertEqual(event["used_reinstatements"], 1)
        self.assertEqual(event["received_premium"], 108000.0)

    def test_legacy_data_backfill_keeps_original_states(self):
        # 旧版结构库路径（不经过setUp中的新版建表）：无事件台账/占用表，payload无reinstatement_count
        legacy_db_path = str(Path(self.temp.name) / "legacy.db")
        connection = sqlite3.connect(legacy_db_path)
        connection.executescript(
            """
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, reference TEXT NOT NULL UNIQUE,
                state TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, payload TEXT NOT NULL,
                created_by TEXT NOT NULL, updated_by TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, record_id INTEGER NOT NULL,
                action TEXT NOT NULL, actor_id TEXT NOT NULL, version INTEGER NOT NULL,
                details TEXT NOT NULL, created_at TEXT NOT NULL
            );
            """
        )

        def payload(**over):
            base = {
                "event_id": "CAT-OLD", "attachment": 1000000.0, "limit": 5000000.0,
                "cession_pct": 0.4, "loss_amount": 3000000.0, "layer_width": 4000000.0,
                "recoverable_amount": 720000.0, "reinstatement_pct": 0.15,
                "reinstatement_premium": 108000.0, "aggregate_prior": 0.0,
            }
            base.update(over)
            return base

        legacy = [
            ("RI-OLD-1", "settled", payload(claim_number="C1", payment_reference="PAY-1")),
            ("RI-OLD-2", "calculated", payload(claim_number="C2", recoverable_amount=400000.0, reinstatement_premium=60000.0)),
            ("RI-OLD-3", "rejected", payload(claim_number="C3")),
        ]
        for reference, state, p in legacy:
            connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at)"
                " VALUES(?,?,1,?, 'legacy','legacy','2026-09-01T00:00:00+00:00','2026-09-02T00:00:00+00:00')",
                (reference, state, json.dumps(p)),
            )
        connection.commit()
        connection.close()

        service = build_service(legacy_db_path)
        event = service.get_event(ADMIN, "CAT-OLD")
        # 旧数据缺省1次，但已耗1+预占1，回填时次数抬到2以保持账实一致
        self.assertEqual(event["total_reinstatements"], 2)
        self.assertEqual(event["used_reinstatements"], 1)
        self.assertEqual(event["reserved_reinstatements"], 1)
        self.assertEqual(event["settled_amount"], 720000.0)
        self.assertEqual(event["received_premium"], 108000.0)
        self.assertEqual(event["reserved_amount"], 400000.0)
        statuses = {o["reference"]: o["status"] for o in event["occupancies"]}
        self.assertEqual(statuses, {"RI-OLD-1": "consumed", "RI-OLD-2": "reserved"})

        # 原状态不变，核定中的旧案件可直接结算
        record = service.get_record(ADMIN, 2)
        self.assertEqual(record["state"], "calculated")
        record = service.act(FN, record["id"], record["version"], "settle", {"payment_reference": "PAY-2"})
        event = service.get_event(ADMIN, "CAT-OLD")
        self.assertEqual(event["used_reinstatements"], 2)
        self.assertEqual(event["received_premium"], 168000.0)

        # 再次重开核对幂等
        build_service(legacy_db_path)
        event = build_service(legacy_db_path).get_event(ADMIN, "CAT-OLD")
        self.assertEqual(event["used_reinstatements"], 2)
        self.assertEqual(len(event["occupancies"]), 2)


if __name__ == "__main__":
    unittest.main()
