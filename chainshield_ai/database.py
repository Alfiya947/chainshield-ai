"""SQLite persistence for ChainShield AI (synthetic data only).

Tables
  transactions      synthetic transaction history the tools investigate
  approval_records  simulated approval register
  cases             one row per investigation (upserted as the case progresses)
  audit_events      append-only timeline for every case
  request_status    the simulated 'ledger' the verification step re-reads
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = Path(__file__).with_name("chainshield_v2.db")

SEED_TRANSACTIONS = [
    # id, order, account, employee, amount, days_ago
    ("TX-1007", "ORD-778", "AC-201", "E-102", 4500, 12),
    ("TX-1011", "ORD-778", "AC-201", "E-107", 4500, 9),
    ("TX-1015", "ORD-991", "AC-338", "E-102", 2100, 20),
    ("TX-1020", "ORD-110", "AC-410", "E-115", 1800, 40),
    ("TX-1024", "ORD-992", "AC-338", "E-102", 1200, 6),
    ("TX-1050", "ORD-501", "AC-905", "E-130", 3900, 10),
    ("TX-1051", "ORD-502", "AC-905", "E-130", 4100, 8),
    ("TX-1052", "ORD-503", "AC-905", "E-130", 3600, 5),
]
SEED_APPROVALS = [
    ("APR-9001", "finance_manager", 50000, 1),
    ("APR-9002", "finance_manager", 25000, 1),
    ("APR-9003", "finance_manager", 50000, 0),   # revoked
    ("APR-9004", "support_agent", 50000, 1),     # issued by a role that may not approve
]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path | None = None):
        self.path = str(path or DEFAULT_DB)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.init()
        self.seed_demo_data()

    # -- plumbing ---------------------------------------------------------- #
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _exec(self, sql: str, params: tuple = ()) -> None:
        with closing(self._conn()) as conn, conn:
            conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        with closing(self._conn()) as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def init(self) -> None:
        with closing(self._conn()) as conn, conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY, original_transaction_id TEXT, account_ref TEXT,
                employee_id TEXT, amount REAL, type TEXT, status TEXT, days_ago INTEGER);
            CREATE TABLE IF NOT EXISTS approval_records (
                approval_ref TEXT PRIMARY KEY, approver_role TEXT, max_amount REAL, valid INTEGER);
            CREATE TABLE IF NOT EXISTS cases (
                case_id TEXT PRIMARY KEY, transaction_id TEXT, account_ref TEXT, original_transaction_id TEXT,
                amount REAL, decision TEXT, risk_score INTEGER, case_outcome TEXT, action_status TEXT,
                verification_status TEXT, created_at TEXT, updated_at TEXT, report_json TEXT);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, case_id TEXT, seq INTEGER, timestamp TEXT,
                event TEXT, detail TEXT, UNIQUE(case_id, seq));
            CREATE TABLE IF NOT EXISTS request_status (
                case_id TEXT PRIMARY KEY, transaction_id TEXT, status TEXT, updated_at TEXT);
            """)

    def seed_demo_data(self) -> None:
        with closing(self._conn()) as conn, conn:
            if conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0:
                conn.executemany(
                    "INSERT INTO transactions VALUES (?,?,?,?,?,'refund','COMPLETED',?)",
                    [(t, o, a, e, amt, d) for t, o, a, e, amt, d in SEED_TRANSACTIONS])
            if conn.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0] == 0:
                conn.executemany("INSERT INTO approval_records VALUES (?,?,?,?)", SEED_APPROVALS)

    # -- reference data ---------------------------------------------------- #
    def get_history(self) -> list[dict]:
        return self._all("SELECT * FROM transactions ORDER BY days_ago")

    def get_approvals(self) -> dict:
        return {r["approval_ref"]: r for r in self._all("SELECT * FROM approval_records")}

    # -- cases / memory ---------------------------------------------------- #
    def save_case(self, report: dict) -> None:
        tx = report["transaction"]
        now = _utc()
        with closing(self._conn()) as conn, conn:
            conn.execute("""
                INSERT INTO cases (case_id, transaction_id, account_ref, original_transaction_id, amount,
                    decision, risk_score, case_outcome, action_status, verification_status,
                    created_at, updated_at, report_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(case_id) DO UPDATE SET
                    case_outcome=excluded.case_outcome, action_status=excluded.action_status,
                    verification_status=excluded.verification_status, updated_at=excluded.updated_at,
                    report_json=excluded.report_json""",
                (report["case_id"], report["transaction_id"], tx.get("account_ref", ""),
                 tx.get("original_transaction_id", ""), tx["amount"], report["decision"],
                 report["risk_score"], report.get("case_outcome", "OPEN"), report.get("action_status", ""),
                 report.get("verification_status", ""), report["started_at"], now,
                 json.dumps(report, ensure_ascii=False, default=str)))
            for e in report.get("audit_events", []):
                conn.execute("INSERT OR IGNORE INTO audit_events (case_id, seq, timestamp, event, detail) VALUES (?,?,?,?,?)",
                             (report["case_id"], e["seq"], e["timestamp"], e["event"], e["detail"]))

    def list_cases(self, limit: int = 200) -> list[dict]:
        return self._all("""SELECT case_id, transaction_id, account_ref, original_transaction_id, amount, decision,
                            risk_score, case_outcome, action_status, verification_status, created_at, updated_at
                            FROM cases ORDER BY created_at DESC, rowid DESC LIMIT ?""", (limit,))

    def dashboard_stats(self) -> dict:
        """Return persisted decision metrics without treating missing data as a successful result."""
        with closing(self._conn()) as conn:
            rows = conn.execute("SELECT decision, report_json FROM cases").fetchall()
            blocked_attempts = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE event='action_refused'"
            ).fetchone()[0]
        violations = 0
        for row in rows:
            report = json.loads(row["report_json"])
            violations += sum(step.get("status") == "VIOLATION" for step in report.get("steps", []))
        total = len(rows)
        allowed = sum(row["decision"] == "ALLOW" for row in rows)
        return {
            "workflows_analyzed": total,
            "unsafe_actions_blocked": blocked_attempts,
            "policy_violations": violations,
            "safe_rate": allowed * 100 / total if total else None,
        }

    def get_case(self, case_id: str) -> dict | None:
        rows = self._all("SELECT report_json FROM cases WHERE case_id=?", (case_id,))
        return json.loads(rows[0]["report_json"]) if rows else None

    def list_memory(self, limit: int = 200) -> list[dict]:
        """Earlier cases, used by the 'similar cases' tool."""
        return self.list_cases(limit)

    def list_audit_events(self, case_id: str | None = None, limit: int = 500) -> list[dict]:
        if case_id:
            return self._all("SELECT * FROM audit_events WHERE case_id=? ORDER BY seq", (case_id,))
        return self._all("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,))

    def clear_cases(self) -> None:
        with closing(self._conn()) as conn, conn:
            for t in ("cases", "audit_events", "request_status"):
                conn.execute(f"DELETE FROM {t}")

    # -- simulated ledger (what verification re-reads) --------------------- #
    def register_case(self, report: dict) -> None:
        self._exec("INSERT OR IGNORE INTO request_status VALUES (?,?,?,?)",
                   (report["case_id"], report["transaction_id"], "PENDING", _utc()))

    def read_status(self, case_id: str) -> str | None:
        rows = self._all("SELECT status FROM request_status WHERE case_id=?", (case_id,))
        return rows[0]["status"] if rows else None

    def write_status(self, case_id: str, transaction_id: str, status: str, simulate_failure: bool = False) -> None:
        """simulate_failure models a downstream system that acknowledges but never persists the change."""
        if simulate_failure:
            return
        self._exec("""INSERT INTO request_status VALUES (?,?,?,?)
                      ON CONFLICT(case_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at""",
                   (case_id, transaction_id, status, _utc()))
