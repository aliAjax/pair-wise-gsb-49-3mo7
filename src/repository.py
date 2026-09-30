"""SQLite 表结构与事务访问。

事件暴露以 event_exposures 为台账：首案确定事件容量与恢复次数；
赔案核定（calculate）预先占用额度与恢复次数，结算（settle）才消耗
次数并累计保费，未结算案件拒赔/撤销时释放占用。event_occupancies
逐笔留档占用明细，供并发竞争时返回剩余次数与占用情况。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import CAPACITY_STATES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _money(value: Any) -> float:
    return round(max(0.0, float(value or 0.0)), 2)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_exposures (
                    event_id TEXT PRIMARY KEY,
                    layer_capacity REAL NOT NULL,
                    total_reinstatements INTEGER NOT NULL,
                    used_reinstatements INTEGER NOT NULL DEFAULT 0,
                    reserved_reinstatements INTEGER NOT NULL DEFAULT 0,
                    reserved_amount REAL NOT NULL DEFAULT 0,
                    settled_amount REAL NOT NULL DEFAULT 0,
                    received_premium REAL NOT NULL DEFAULT 0,
                    prior_amount REAL NOT NULL DEFAULT 0,
                    anchor_record_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_occupancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    record_id INTEGER NOT NULL UNIQUE,
                    reference TEXT NOT NULL DEFAULT '',
                    claim_number TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    reserved_amount REAL NOT NULL DEFAULT 0,
                    settled_amount REAL NOT NULL DEFAULT 0,
                    reinstatement_premium REAL NOT NULL DEFAULT 0,
                    payment_reference TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_occupancy_event ON event_occupancies(event_id, status);
                """
            )
        # 旧数据回填与写入中断后的占用核对，按同一事件幂等恢复。
        self.reconcile_events()

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _occupancy_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        for key in ("reserved_amount", "settled_amount", "reinstatement_premium"):
            item[key] = round(float(item[key]), 2)
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _check_version(row: Optional[sqlite3.Row], expected_version: int) -> None:
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")

    def _write_record(self, connection: sqlite3.Connection, row: sqlite3.Row, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any], now: str) -> Dict[str, Any]:
        version = int(row["version"]) + 1
        connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, row["id"]),
        )
        connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (row["id"], action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
        )
        result = connection.execute("SELECT * FROM records WHERE id=?", (row["id"],)).fetchone()
        return self._row(result)

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """不涉及事件台账占用的普通流转（bind）。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._check_version(row, expected_version)
            result = self._write_record(connection, row, state, payload, actor_id, action, details, now)
            connection.commit()
        return result

    @staticmethod
    def _event_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
        width = float(payload.get("layer_width", float(payload.get("limit", 0)) - float(payload.get("attachment", 0))))
        cession = float(payload.get("cession_pct", 0))
        return {
            "capacity": _money(width * cession),
            "total": int(payload.get("reinstatement_count", 1) or 0),
            "prior": _money(payload.get("aggregate_prior", 0)),
        }

    def _recompute_event(self, connection: sqlite3.Connection, event_id: str, now: str) -> None:
        """以占用明细为唯一事实来源重算台账汇总，保证次数/保费/额度恒等。"""
        connection.execute(
            """
            UPDATE event_exposures SET
                used_reinstatements = (SELECT COUNT(*) FROM event_occupancies WHERE event_id=? AND status='consumed'),
                reserved_reinstatements = (SELECT COUNT(*) FROM event_occupancies WHERE event_id=? AND status='reserved'),
                reserved_amount = (SELECT COALESCE(SUM(reserved_amount),0) FROM event_occupancies WHERE event_id=? AND status='reserved'),
                settled_amount = (SELECT COALESCE(SUM(settled_amount),0) FROM event_occupancies WHERE event_id=? AND status='consumed'),
                received_premium = (SELECT COALESCE(SUM(reinstatement_premium),0) FROM event_occupancies WHERE event_id=? AND status='consumed'),
                updated_at=?
            WHERE event_id=?
            """,
            (event_id, event_id, event_id, event_id, event_id, now, event_id),
        )

    def _event_detail(self, connection: sqlite3.Connection, event_id: str) -> Dict[str, Any]:
        row = connection.execute("SELECT * FROM event_exposures WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound("事件台账不存在")
        event = dict(row)
        for key in ("layer_capacity", "reserved_amount", "settled_amount", "received_premium", "prior_amount"):
            event[key] = round(float(event[key]), 2)
        event["remaining_reinstatements"] = max(0, int(event["total_reinstatements"]) - int(event["used_reinstatements"]) - int(event["reserved_reinstatements"]))
        occupied = float(event["prior_amount"]) + float(event["settled_amount"]) + float(event["reserved_amount"])
        event["available_capacity"] = round(max(0.0, float(event["layer_capacity"]) - occupied), 2)
        return event

    def _occupancies(self, connection: sqlite3.Connection, event_id: str) -> List[Dict[str, Any]]:
        rows = connection.execute("SELECT * FROM event_occupancies WHERE event_id=? ORDER BY id", (event_id,)).fetchall()
        return [self._occupancy_row(row) for row in rows]

    def _conflict_details(self, connection: sqlite3.Connection, event_id: str, amount: float, reason: str) -> Dict[str, Any]:
        event = self._event_detail(connection, event_id)
        return {
            "reason": reason,
            "event_id": event_id,
            "remaining_reinstatements": event["remaining_reinstatements"],
            "total_reinstatements": event["total_reinstatements"],
            "used_reinstatements": event["used_reinstatements"],
            "reserved_reinstatements": event["reserved_reinstatements"],
            "layer_capacity": event["layer_capacity"],
            "available_capacity": event["available_capacity"],
            "requested_amount": _money(amount),
            "occupancies": self._occupancies(connection, event_id),
        }

    def submit_claim(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """提交赔案：首案确定事件容量与恢复次数（幂等建账）。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._check_version(row, expected_version)
            event_id = str(payload.get("claim_event_id") or payload.get("event_id"))
            event_row = connection.execute("SELECT * FROM event_exposures WHERE event_id=?", (event_id,)).fetchone()
            if event_row is None:
                anchor = self._row(row)
                spec = self._event_payload(anchor["payload"])
                connection.execute(
                    """
                    INSERT INTO event_exposures(event_id,layer_capacity,total_reinstatements,prior_amount,anchor_record_id,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?)
                    """,
                    (event_id, spec["capacity"], spec["total"], spec["prior"], record_id, now, now),
                )
                details["event_ledger"] = {"created": True, "event_id": event_id, "layer_capacity": spec["capacity"], "total_reinstatements": spec["total"]}
            result = self._write_record(connection, row, state, payload, actor_id, action, details, now)
            connection.commit()
        return result

    def calculate_claim(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """核定赔案：在事件台账上预先占用额度与一次恢复次数；竞争最后一次时只许一笔成功。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._check_version(row, expected_version)
            record = self._row(row)
            event_id = str(payload.get("claim_event_id") or payload.get("event_id"))
            event_row = connection.execute("SELECT * FROM event_exposures WHERE event_id=?", (event_id,)).fetchone()
            if event_row is None:
                # 台账缺失（旧库/异常中断）先按本事件重开核对再继续。
                connection.rollback()
                self.reconcile_events()
                return self.calculate_claim(record_id, expected_version, state, payload, actor_id, action, details)
            amount = _money(payload.get("recoverable_amount"))
            premium = _money(payload.get("reinstatement_premium"))
            occupancies = self._occupancies(connection, event_id)
            self_row = next((item for item in occupancies if int(item["record_id"]) == record_id), None)
            if self_row is not None and self_row["status"] == "consumed":
                raise Conflict("该赔案已结算占用，不能重复核定", self._conflict_details(connection, event_id, amount, "already_settled"))
            others = [item for item in occupancies if int(item["record_id"]) != record_id and item["status"] != "released"]
            reserved_others = sum(item["reserved_amount"] for item in others if item["status"] == "reserved")
            used_slots = sum(1 for item in others if item["status"] == "consumed")
            reserved_slots = sum(1 for item in others if item["status"] == "reserved")
            event = self._event_detail(connection, event_id)
            if int(event["total_reinstatements"]) - used_slots - reserved_slots < 1:
                raise Conflict(
                    "恢复次数不足，剩余0次",
                    self._conflict_details(connection, event_id, amount, "no_reinstatement_left"),
                )
            committed = float(event["prior_amount"]) + float(event["settled_amount"]) + reserved_others
            if committed + amount > float(event["layer_capacity"]) + 0.01:
                raise Conflict(
                    "事件可用额度不足，剩余%.2f" % max(0.0, float(event["layer_capacity"]) - committed),
                    self._conflict_details(connection, event_id, amount, "insufficient_capacity"),
                )
            claim_number = str(payload.get("claim_number", ""))
            if self_row is None:
                connection.execute(
                    """
                    INSERT INTO event_occupancies(event_id,record_id,reference,claim_number,status,reserved_amount,created_at,updated_at)
                    VALUES(?,?,?,?, 'reserved', ?, ?, ?)
                    """,
                    (event_id, record_id, record.get("reference", ""), claim_number, amount, now, now),
                )
            else:
                connection.execute(
                    "UPDATE event_occupancies SET status='reserved',reserved_amount=?,claim_number=?,updated_at=? WHERE record_id=?",
                    (amount, claim_number, now, record_id),
                )
            self._recompute_event(connection, event_id, now)
            ledger = self._event_detail(connection, event_id)
            details["event_ledger"] = {"event_id": event_id, "reserved_amount": amount, "remaining_reinstatements": ledger["remaining_reinstatements"], "available_capacity": ledger["available_capacity"]}
            result = self._write_record(connection, row, state, payload, actor_id, action, details, now)
            connection.commit()
        return result

    def settle_claim(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """结算：占用转为已消耗，扣减恢复次数、累计摊回与已收保费，付款留档。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._check_version(row, expected_version)
            record = self._row(row)
            event_id = str(payload.get("claim_event_id") or payload.get("event_id"))
            amount = _money(payload.get("settled_recovery", payload.get("recoverable_amount")))
            premium = _money(payload.get("settled_premium", payload.get("reinstatement_premium")))
            payment_reference = str(payload.get("payment_reference", ""))
            occupancy = connection.execute("SELECT * FROM event_occupancies WHERE record_id=?", (record_id,)).fetchone()
            if occupancy is None:
                # 占用明细缺失（写入中断/旧库未回填）：按同一事件重开核对后再结算。
                connection.rollback()
                self.reconcile_events()
                return self.settle_claim(record_id, expected_version, state, payload, actor_id, action, details)
            connection.execute(
                """
                UPDATE event_occupancies
                SET status='consumed', settled_amount=?, reinstatement_premium=?, payment_reference=?, updated_at=?
                WHERE record_id=?
                """,
                (amount, premium, payment_reference, now, record_id),
            )
            self._recompute_event(connection, event_id, now)
            ledger = self._event_detail(connection, event_id)
            details["event_ledger"] = {"event_id": event_id, "consumed_amount": amount, "received_premium": premium, "used_reinstatements": ledger["used_reinstatements"], "received_premium_total": ledger["received_premium"]}
            result = self._write_record(connection, row, state, payload, actor_id, action, details, now)
            connection.commit()
        return result

    def resolve_claim(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """未结算案件拒赔或撤销：释放预先占用，次数与额度归还事件台账。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            self._check_version(row, expected_version)
            event_id = str(payload.get("claim_event_id") or payload.get("event_id"))
            occupancy = connection.execute("SELECT * FROM event_occupancies WHERE record_id=?", (record_id,)).fetchone()
            released = 0.0
            if occupancy is not None and str(occupancy["status"]) == "reserved":
                released = round(float(occupancy["reserved_amount"]), 2)
                connection.execute(
                    "UPDATE event_occupancies SET status='released', updated_at=? WHERE record_id=?",
                    (now, record_id),
                )
                self._recompute_event(connection, event_id, now)
            ledger = self._event_detail(connection, event_id)
            details["event_ledger"] = {"event_id": event_id, "released_amount": released, "remaining_reinstatements": ledger["remaining_reinstatements"], "available_capacity": ledger["available_capacity"]}
            result = self._write_record(connection, row, state, payload, actor_id, action, details, now)
            connection.commit()
        return result

    def reconcile_events(self) -> Dict[str, int]:
        """按事件重开核对：旧数据回填事件关联，修复写入中断留下的占用。幂等。"""
        now = _now()
        summary = {"events": 0, "events_created": 0, "occupancies_created": 0, "occupancies_repaired": 0, "orphans_released": 0}
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            records = connection.execute("SELECT * FROM records ORDER BY id").fetchall()
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for raw in records:
                item = self._row(raw)
                event_id = item["payload"].get("claim_event_id") or item["payload"].get("event_id")
                if not event_id or item["state"] not in CAPACITY_STATES | {"claim_submitted", "rejected", "cancelled"}:
                    continue
                grouped.setdefault(str(event_id), []).append(item)
            for event_id, items in grouped.items():
                summary["events"] += 1
                anchor = items[0]
                spec = self._event_payload(anchor["payload"])
                event_row = connection.execute("SELECT * FROM event_exposures WHERE event_id=?", (event_id,)).fetchone()
                if event_row is None:
                    needed = sum(1 for item in items if item["state"] in CAPACITY_STATES)
                    total = max(spec["total"], needed)
                    connection.execute(
                        """
                        INSERT INTO event_exposures(event_id,layer_capacity,total_reinstatements,prior_amount,anchor_record_id,created_at,updated_at)
                        VALUES(?,?,?,?,?,?,?)
                        """,
                        (event_id, spec["capacity"], total, spec["prior"], anchor["id"], now, now),
                    )
                    summary["events_created"] += 1
                for item in items:
                    p = item["payload"]
                    occupancy = connection.execute("SELECT * FROM event_occupancies WHERE record_id=?", (item["id"],)).fetchone()
                    if item["state"] == "settled":
                        amount = _money(p.get("settled_recovery", p.get("recoverable_amount")))
                        premium = _money(p.get("settled_premium", p.get("reinstatement_premium")))
                        payment = str(p.get("payment_reference", ""))
                        if occupancy is None:
                            connection.execute(
                                """
                                INSERT INTO event_occupancies(event_id,record_id,reference,claim_number,status,reserved_amount,settled_amount,reinstatement_premium,payment_reference,created_at,updated_at)
                                VALUES(?,?,?,?, 'consumed', ?,?,?,?,?,?)
                                """,
                                (event_id, item["id"], p.get("reference", item.get("reference", "")), str(p.get("claim_number", "")), amount, amount, premium, payment, now, now),
                            )
                            summary["occupancies_created"] += 1
                        elif str(occupancy["status"]) != "consumed":
                            connection.execute(
                                "UPDATE event_occupancies SET status='consumed',reserved_amount=?,settled_amount=?,reinstatement_premium=?,payment_reference=?,updated_at=? WHERE record_id=?",
                                (amount, amount, premium, payment, now, item["id"]),
                            )
                            summary["occupancies_repaired"] += 1
                    elif item["state"] == "calculated":
                        amount = _money(p.get("recoverable_amount"))
                        if occupancy is None:
                            connection.execute(
                                """
                                INSERT INTO event_occupancies(event_id,record_id,reference,claim_number,status,reserved_amount,created_at,updated_at)
                                VALUES(?,?,?,?, 'reserved', ?, ?, ?)
                                """,
                                (event_id, item["id"], p.get("reference", item.get("reference", "")), str(p.get("claim_number", "")), amount, now, now),
                            )
                            summary["occupancies_created"] += 1
                        elif str(occupancy["status"]) != "reserved":
                            connection.execute(
                                "UPDATE event_occupancies SET status='reserved',reserved_amount=COALESCE(NULLIF(reserved_amount,0),?),updated_at=? WHERE record_id=?",
                                (amount, now, item["id"]),
                            )
                            summary["occupancies_repaired"] += 1
                    else:
                        # claim_submitted / rejected / cancelled：未核定不占额度，残留占用按中断孤儿释放。
                        if occupancy is not None and str(occupancy["status"]) == "reserved":
                            connection.execute(
                                "UPDATE event_occupancies SET status='released',updated_at=? WHERE record_id=?",
                                (now, item["id"]),
                            )
                            summary["orphans_released"] += 1
                self._recompute_event(connection, event_id, now)
            connection.commit()
        return summary

    def get_event(self, event_id: str) -> Dict[str, Any]:
        with self._connect() as connection:
            event = self._event_detail(connection, event_id)
            event["occupancies"] = self._occupancies(connection, event_id)
        return event

    def list_events(self) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM event_exposures ORDER BY event_id").fetchall()
            result = []
            for row in rows:
                event_id = str(row["event_id"])
                item = self._event_detail(connection, event_id)
                item["occupancies"] = self._occupancies(connection, event_id)
                result.append(item)
        return result

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
