"""SQLite 表结构、事务与事件暴露台账访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    capacity REAL NOT NULL,
                    reinstatements INTEGER NOT NULL,
                    occupied_amount REAL NOT NULL DEFAULT 0,
                    settled_amount REAL NOT NULL DEFAULT 0,
                    reinstatements_used INTEGER NOT NULL DEFAULT 0,
                    premium_received REAL NOT NULL DEFAULT 0,
                    first_record_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    record_id INTEGER NOT NULL,
                    claim_number TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    amount REAL NOT NULL DEFAULT 0,
                    premium REAL NOT NULL DEFAULT 0,
                    payment_reference TEXT NOT NULL DEFAULT '',
                    actor_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, record_id)
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
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_occupancies_event ON occupancies(event_id, id);
                CREATE INDEX IF NOT EXISTS idx_events_event ON events(event_id);
                """
            )
            self._backfill_ledger(connection)

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    # ------------------------------------------------------------------
    # 旧数据回填：旧库没有事件关联时，按记录原状态重建台账，不改动记录本身。
    # ------------------------------------------------------------------
    def _backfill_ledger(self, connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT id,state,payload FROM records ORDER BY id").fetchall()
        if not rows:
            return
        now = _now()
        events: Dict[str, Dict[str, Any]] = {}
        entries: List[tuple] = []
        for row in rows:
            payload = json.loads(row["payload"])
            event_id = payload.get("event_id")
            if not event_id:
                continue
            if event_id not in events:
                width = float(payload.get("layer_width", max(0.0, float(payload.get("limit", 0)) - float(payload.get("attachment", 0)))))
                capacity = round(width * float(payload.get("cession_pct", 0)), 2)
                events[event_id] = {"capacity": capacity, "reinstatements": int(payload.get("reinstatements", 1)), "first_record_id": row["id"]}
            if row["state"] in ("calculated", "settled"):
                amount = round(float(payload.get("recoverable_amount", 0)), 2)
                premium = round(float(payload.get("reinstatement_premium", 0)), 2)
                state = "settled" if row["state"] == "settled" else "reserved"
                entries.append(
                    (event_id, row["id"], payload.get("claim_number", ""), state, amount,
                     premium if state == "settled" else 0.0, payload.get("payment_reference", ""), now)
                )
        for event_id, info in events.items():
            connection.execute(
                "INSERT OR IGNORE INTO events(event_id,capacity,reinstatements,occupied_amount,settled_amount,"
                "reinstatements_used,premium_received,first_record_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, info["capacity"], info["reinstatements"], 0.0, 0.0, 0, 0.0, info["first_record_id"], now, now),
            )
        for entry in entries:
            event_id, record_id, claim_number, state, amount, premium, payment_reference, ts = entry
            connection.execute(
                "INSERT OR IGNORE INTO occupancies(event_id,record_id,claim_number,state,amount,premium,"
                "payment_reference,actor_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, record_id, claim_number, state, amount, premium, payment_reference, "backfill", ts, ts),
            )
            if state == "settled":
                connection.execute(
                    "UPDATE events SET occupied_amount=occupied_amount+?, settled_amount=settled_amount+?, "
                    "reinstatements_used=reinstatements_used+1, premium_received=premium_received+?, updated_at=? "
                    "WHERE event_id=?",
                    (amount, amount, premium, ts, event_id),
                )
            else:
                connection.execute(
                    "UPDATE events SET occupied_amount=occupied_amount+?, updated_at=? WHERE event_id=?",
                    (amount, ts, event_id),
                )

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

    # ------------------------------------------------------------------
    # 事件暴露台账只读视图
    # ------------------------------------------------------------------
    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def list_events(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM events ORDER BY id LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def event_details(self, event_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                return None
            items = connection.execute(
                "SELECT o.*, r.reference AS record_reference FROM occupancies o "
                "LEFT JOIN records r ON r.id=o.record_id WHERE o.event_id=? ORDER BY o.id",
                (event_id,),
            ).fetchall()
        event = dict(row)
        capacity = float(event["capacity"])
        reinstatements = int(event["reinstatements"])
        used = int(event["reinstatements_used"])
        return {
            "event_id": event_id,
            "capacity": round(capacity, 2),
            "reinstatements": reinstatements,
            "reinstatements_remaining": max(0, reinstatements - used),
            "reinstatements_used": used,
            "occupied_amount": round(float(event["occupied_amount"]), 2),
            "available_amount": round(capacity - float(event["occupied_amount"]), 2),
            "settled_amount": round(float(event["settled_amount"]), 2),
            "premium_received": round(float(event["premium_received"]), 2),
            "occupancies": [
                {
                    "record_id": int(item["record_id"]),
                    "reference": item["record_reference"],
                    "claim_number": item["claim_number"],
                    "state": item["state"],
                    "amount": round(float(item["amount"]), 2),
                    "premium": round(float(item["premium"]), 2),
                    "payment_reference": item["payment_reference"],
                }
                for item in (dict(r) for r in items)
            ],
        }

    def ledger_gateway(self) -> "LedgerGateway":
        """开启一个立即事务，供服务层在同一事务内核对台账与更新赔案。"""
        return LedgerGateway(self._connect())


class LedgerGateway:
    """单事务网关：BEGIN IMMEDIATE 保证跨赔案的台账核对串行化。

    所有写操作在同一个SQLite连接/事务内完成，commit后才释放写锁，
    因此两笔赔案同时抢最后一次恢复时只有一笔能成功。
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self.committed = False

    def __enter__(self) -> "LedgerGateway":
        self.connection.execute("BEGIN IMMEDIATE")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self.committed:
            self.connection.rollback()
        self.connection.close()

    def commit(self) -> None:
        self.connection.commit()
        self.committed = True

    # ---- 赔案记录 ----------------------------------------------------
    def get_record(self, record_id: int) -> Dict[str, Any]:
        row = self.connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return Repository._row(row)

    def insert_record(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            cursor = self.connection.execute(
                "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
            )
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        record_id = int(cursor.lastrowid)
        self.add_audit(record_id, actor_id, "created", {"state": state}, 1)
        return self.get_record(record_id)

    def update_record(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any],
                      actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        row = self.connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        if int(row["version"]) != int(expected_version):
            raise Conflict("版本冲突，请刷新后重试")
        version = int(expected_version) + 1
        now = _now()
        self.connection.execute(
            "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
            (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
        )
        self.add_audit(record_id, actor_id, action, details, version)
        return self.get_record(record_id)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any], version: int = None) -> None:
        if version is None:
            row = self.connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            version = int(row["version"])
        self.connection.execute(
            "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
            (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    # ---- 事件台账 ----------------------------------------------------
    def get_event(self, event_id: str) -> Optional[Dict[str, Any]]:
        row = self.connection.execute("SELECT * FROM events WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def ensure_event(self, event_id: str, capacity: float, reinstatements: int,
                     first_record_id: int, actor_id: str) -> Dict[str, Any]:
        """首案确定事件容量与恢复次数；已存在则不改动首案参数。"""
        existing = self.get_event(event_id)
        if existing is not None:
            return existing
        now = _now()
        self.connection.execute(
            "INSERT INTO events(event_id,capacity,reinstatements,occupied_amount,settled_amount,"
            "reinstatements_used,premium_received,first_record_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (event_id, capacity, reinstatements, 0.0, 0.0, 0, 0.0, first_record_id, now, now),
        )
        return self.get_event(event_id)

    def adjust_event(self, event_id: str, occupied_delta: float = 0.0, settled_delta: float = 0.0,
                     used_delta: int = 0, premium_delta: float = 0.0, actor_id: str = "") -> None:
        self.connection.execute(
            "UPDATE events SET occupied_amount=ROUND(occupied_amount+?,2), settled_amount=ROUND(settled_amount+?,2), "
            "reinstatements_used=reinstatements_used+?, premium_received=ROUND(premium_received+?,2), updated_at=? "
            "WHERE event_id=?",
            (round(occupied_delta, 2), round(settled_delta, 2), used_delta, round(premium_delta, 2), _now(), event_id),
        )

    # ---- 占用明细 ----------------------------------------------------
    def get_occupancy(self, event_id: str, record_id: int) -> Optional[Dict[str, Any]]:
        row = self.connection.execute(
            "SELECT * FROM occupancies WHERE event_id=? AND record_id=?", (event_id, record_id)
        ).fetchone()
        return dict(row) if row else None

    def list_occupancies(self, event_id: str) -> List[Dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT o.*, r.reference AS record_reference FROM occupancies o "
            "LEFT JOIN records r ON r.id=o.record_id WHERE o.event_id=? ORDER BY o.id",
            (event_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def upsert_occupancy(self, event_id: str, record_id: int, state: str, amount: float, premium: float,
                         actor_id: str, claim_number: str = "", payment_reference: str = "") -> None:
        now = _now()
        existing = self.get_occupancy(event_id, record_id)
        if existing is None:
            self.connection.execute(
                "INSERT INTO occupancies(event_id,record_id,claim_number,state,amount,premium,payment_reference,"
                "actor_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (event_id, record_id, claim_number, state, round(amount, 2), round(premium, 2),
                 payment_reference, actor_id, now, now),
            )
        else:
            self.connection.execute(
                "UPDATE occupancies SET state=?, amount=?, premium=?, payment_reference=?, actor_id=?, "
                "claim_number=COALESCE(NULLIF(?, ''), claim_number), updated_at=? WHERE event_id=? AND record_id=?",
                (state, round(amount, 2), round(premium, 2), payment_reference, actor_id, claim_number, now,
                 event_id, record_id),
            )

    def event_details(self, event_id: str) -> Dict[str, Any]:
        """容量冲突时返回给调用方的剩余次数与占用明细。"""
        event = self.get_event(event_id)
        if event is None:
            return {}
        capacity = float(event["capacity"])
        reinstatements = int(event["reinstatements"])
        used = int(event["reinstatements_used"])
        occupied = float(event["occupied_amount"])
        settled = float(event["settled_amount"])
        items = self.list_occupancies(event_id)
        return {
            "event_id": event_id,
            "capacity": round(capacity, 2),
            "reinstatements": reinstatements,
            "reinstatements_remaining": max(0, reinstatements - used),
            "reinstatements_used": used,
            "occupied_amount": round(occupied, 2),
            "available_amount": round(capacity - occupied, 2),
            "settled_amount": round(settled, 2),
            "premium_received": round(float(event["premium_received"]), 2),
            "occupancies": [
                {
                    "record_id": int(item["record_id"]),
                    "reference": item["record_reference"],
                    "claim_number": item["claim_number"],
                    "state": item["state"],
                    "amount": round(float(item["amount"]), 2),
                    "premium": round(float(item["premium"]), 2),
                    "payment_reference": item["payment_reference"],
                }
                for item in items
            ],
        }
