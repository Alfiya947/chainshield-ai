"""Demo scenarios shown in the UI. 'expected' doubles as a regression test."""
from __future__ import annotations

SCENARIOS: dict[str, dict] = {
    "Everyday refund (clean)": {
        "description": "A normal ₹3,200 refund from an authorised agent on a fresh account. Expect: cleared.",
        "expected": "ALLOW",
        "transaction": {"transaction_id": "TX-4010", "original_transaction_id": "ORD-401", "account_ref": "AC-610",
                        "employee_id": "E-120", "employee_role": "support_agent", "amount": 3200, "approval_ref": ""},
    },
    "Large refund, no approval (the pitch example)": {
        "description": "₹28,000 refund on an order that was already refunded, with no approval. Expect: human review.",
        "expected": "ESCALATE",
        "transaction": {"transaction_id": "TX-1042", "original_transaction_id": "ORD-778", "account_ref": "AC-201",
                        "employee_id": "E-118", "employee_role": "support_agent", "amount": 28000, "approval_ref": ""},
    },
    "Large refund WITH valid approval": {
        "description": "₹28,000 refund covered by a valid finance-manager approval (APR-9001). Expect: cleared.",
        "expected": "ALLOW",
        "transaction": {"transaction_id": "TX-6001", "original_transaction_id": "ORD-620", "account_ref": "AC-620",
                        "employee_id": "E-121", "employee_role": "support_agent", "amount": 28000, "approval_ref": "APR-9001"},
    },
    "Approval too small for the amount": {
        "description": "₹45,000 refund, but approval APR-9002 only covers ₹25,000. Expect: human review.",
        "expected": "ESCALATE",
        "transaction": {"transaction_id": "TX-6002", "original_transaction_id": "ORD-621", "account_ref": "AC-621",
                        "employee_id": "E-121", "employee_role": "support_agent", "amount": 45000, "approval_ref": "APR-9002"},
    },
    "Unauthorised employee role": {
        "description": "An intern tries to issue a refund. A mandatory rule is broken. Expect: blocked, agent stops early.",
        "expected": "BLOCK",
        "transaction": {"transaction_id": "TX-3099", "original_transaction_id": "ORD-880", "account_ref": "AC-512",
                        "employee_id": "E-999", "employee_role": "intern", "amount": 7200, "approval_ref": ""},
    },
    "Re-used transaction ID": {
        "description": "A request re-uses ID TX-1007, which was already processed. Expect: blocked.",
        "expected": "BLOCK",
        "transaction": {"transaction_id": "TX-1007", "original_transaction_id": "ORD-778", "account_ref": "AC-201",
                        "employee_id": "E-118", "employee_role": "finance_manager", "amount": 4500, "approval_ref": ""},
    },
    "Second refund on the same order": {
        "description": "₹4,500 refund on order ORD-778, which was already refunded twice. Expect: human review.",
        "expected": "ESCALATE",
        "transaction": {"transaction_id": "TX-2041", "original_transaction_id": "ORD-778", "account_ref": "AC-201",
                        "employee_id": "E-118", "employee_role": "support_agent", "amount": 4500, "approval_ref": ""},
    },
    "Rapid refund pattern on one account": {
        "description": "A small refund on an account that already had 3 refunds in 10 days from one employee. Expect: human review.",
        "expected": "ESCALATE",
        "transaction": {"transaction_id": "TX-5001", "original_transaction_id": "ORD-505", "account_ref": "AC-905",
                        "employee_id": "E-130", "employee_role": "support_agent", "amount": 3800, "approval_ref": ""},
    },
    "Incomplete request": {
        "description": "Account and order details are missing, so related records cannot be checked. Expect: human review.",
        "expected": "ESCALATE",
        "transaction": {"transaction_id": "TX-7001", "original_transaction_id": "", "account_ref": "",
                        "employee_id": "E-120", "employee_role": "support_agent", "amount": 2500, "approval_ref": ""},
    },
    "Blank form (type your own)": {
        "description": "Start from empty fields and try your own combination.",
        "expected": None,
        "transaction": {"transaction_id": "TX-9000", "original_transaction_id": "ORD-900", "account_ref": "AC-900",
                        "employee_id": "E-900", "employee_role": "support_agent", "amount": 1000, "approval_ref": ""},
    },
}

APPROVER = {"name": "Priya Nair", "role": "finance_manager"}
