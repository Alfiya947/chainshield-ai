"""ChainShield AI - investigation engine.

No UI code lives here. The engine is split into clear layers:

  1. Input validation      normalize_transaction()
  2. Investigation tools   tool_* functions, registered in TOOL_CATALOG
  3. Agent loop            investigate_transaction(): plan -> run tool -> RE-ASSESS -> repeat
  4. Decision engine       decide(): deterministic rules only. No language model can change it.
  5. Guarded actions       execute_action(): approval gate + closed-loop verification

Finding status vocabulary (used everywhere, including the UI):
  PASS       the check passed
  FLAG       suspicious indicator / unresolved policy condition  -> needs a human
  VIOLATION  definite breach of a mandatory rule                 -> block
  INFO       context only, never affects the decision
  ERROR      the tool itself failed; treated as missing evidence -> needs a human
"""
from __future__ import annotations

import math
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Callable

import networkx as nx

# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
DEFAULT_POLICY: dict[str, Any] = {
    "refund_limit": 20000.0,                       # above this needs documented approval
    "allowed_roles": ["support_agent", "finance_manager"],
    "approver_roles": ["finance_manager", "risk_officer"],
    "repeat_refund_threshold": 2,                  # refunds on one account inside the window
    "history_window_days": 30,
    "cluster_flag_size": 3,                        # linked transactions (incl. this one)
}

MAX_STEPS = 12

ACTIONS: dict[str, dict[str, str]] = {
    "allow": {
        "status": "APPROVED",
        "label": "Release the refund for processing",
        "detail": "Sets the simulated transaction status to APPROVED.",
    },
    "mark_for_review": {
        "status": "UNDER_REVIEW",
        "label": "Place the transaction under review",
        "detail": "Holds it in UNDER_REVIEW until an authorised reviewer clears it.",
    },
    "reject": {
        "status": "REJECTED",
        "label": "Reject the refund request",
        "detail": "Records a policy-based rejection (status REJECTED).",
    },
}

# Which actions policy permits for each decision, and which one is recommended.
ALLOWED_ACTIONS = {
    "ALLOW": ["allow", "mark_for_review"],
    "ESCALATE": ["mark_for_review", "reject"],
    "BLOCK": ["reject", "mark_for_review"],
}
RECOMMENDED_ACTION = {"ALLOW": "allow", "ESCALATE": "mark_for_review", "BLOCK": "reject"}


class InvalidTransaction(ValueError):
    """Raised when the submitted transaction fails input validation."""


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _inr(value: Any) -> str:
    return f"₹{float(value):,.0f}"


def _role_label(role: str) -> str:
    return str(role).replace("_", " ")


def _eq(a: Any, b: Any) -> bool:
    return str(a or "").strip().lower() == str(b or "").strip().lower() and str(a or "").strip() != ""


_ID_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,64}$")


def _clean_id(value: Any, label: str, required: bool = False) -> str:
    text = "" if value is None else str(value).strip()
    if not text:
        if required:
            raise InvalidTransaction(f"{label} is required.")
        return ""
    if not _ID_RE.match(text):
        raise InvalidTransaction(
            f"{label} may only contain letters, numbers and - _ . / (max 64 characters)."
        )
    return text


def normalize_transaction(raw: dict) -> dict:
    """Validate and normalise untrusted input. Raises InvalidTransaction."""
    if not isinstance(raw, dict):
        raise InvalidTransaction("Transaction must be a set of named fields.")
    try:
        amount = float(raw.get("amount"))
    except (TypeError, ValueError):
        raise InvalidTransaction("Refund amount must be a number.") from None
    if not math.isfinite(amount) or amount <= 0:
        raise InvalidTransaction("Refund amount must be greater than zero.")
    if amount > 1_000_000_000:
        raise InvalidTransaction("Refund amount is unrealistically large; check the value.")

    role = str(raw.get("employee_role") or "unknown").strip().lower()
    if not re.match(r"^[a-z_]{1,40}$", role):
        raise InvalidTransaction("Employee role may only contain letters and underscores.")

    return {
        "transaction_id": _clean_id(raw.get("transaction_id"), "Transaction ID", required=True),
        "original_transaction_id": _clean_id(raw.get("original_transaction_id"), "Original order ID"),
        "account_ref": _clean_id(raw.get("account_ref"), "Account reference"),
        "employee_id": _clean_id(raw.get("employee_id"), "Employee ID"),
        "employee_role": role,
        "approval_ref": _clean_id(raw.get("approval_ref"), "Approval reference"),
        "amount": round(amount, 2),
        "type": "refund",
        "status": "PENDING",
    }


def _finding(tool: str, status: str, summary: str, evidence: dict, points: int = 0, **extra) -> dict:
    out = {"tool": tool, "status": status, "summary": summary, "evidence": evidence, "risk_points": points}
    out.update(extra)
    return out


# --------------------------------------------------------------------------- #
# Relationship graph (NetworkX)
# --------------------------------------------------------------------------- #
def build_graph_data(tx: dict, history: list[dict]) -> dict:
    """Graph of the current transaction and everything linked to it.

    Links through a shared account or shared order define the 'cluster'.
    Employees are shown for context but never create a cluster on their own.
    Returns plain dicts so the graph can be stored inside the case report.
    """
    G = nx.Graph()
    cur = "CUR"
    G.add_node(cur, kind="transaction", relation="current", label=tx["transaction_id"], amount=tx["amount"])

    def link(txn_node: str, rec: dict) -> None:
        acct, order, emp = rec.get("account_ref"), rec.get("original_transaction_id"), rec.get("employee_id")
        if acct:
            G.add_node(f"A:{acct}", kind="account", label=str(acct))
            G.add_edge(txn_node, f"A:{acct}", kind="account")
        if order:
            G.add_node(f"O:{order}", kind="order", label=str(order))
            G.add_edge(txn_node, f"O:{order}", kind="order")
        if emp:
            G.add_node(f"E:{emp}", kind="employee", label=str(emp))
            G.add_edge(txn_node, f"E:{emp}", kind="employee")

    link(cur, tx)
    for i, rec in enumerate(history):
        node = f"H{i}"
        if _eq(rec.get("transaction_id"), tx["transaction_id"]):
            relation = "duplicate_id"
        elif _eq(rec.get("original_transaction_id"), tx["original_transaction_id"]):
            relation = "same_order"
        elif _eq(rec.get("account_ref"), tx["account_ref"]):
            relation = "same_account"
        else:
            relation = "other"
        G.add_node(node, kind="transaction", relation=relation, label=str(rec.get("transaction_id")),
                   amount=float(rec.get("amount") or 0), status=rec.get("status", ""))
        link(node, rec)

    structural = nx.Graph()
    structural.add_nodes_from(G.nodes)
    structural.add_edges_from((u, v) for u, v, d in G.edges(data=True) if d["kind"] in ("account", "order"))
    cluster = nx.node_connected_component(structural, cur)

    keep = set(cluster)
    for n in list(cluster):  # add employees touching cluster transactions, for context
        if G.nodes[n]["kind"] == "transaction":
            keep.update(m for m in G.neighbors(n) if G.nodes[m]["kind"] == "employee")
    view = G.subgraph(keep)
    return {
        "nodes": [{"id": n, **d} for n, d in view.nodes(data=True)],
        "edges": [{"source": u, "target": v, "kind": d["kind"]} for u, v, d in view.edges(data=True)],
        "linked_transactions": sum(1 for n in cluster if n != cur and G.nodes[n]["kind"] == "transaction"),
    }


_EDGE_LABEL = {"account": "same account", "order": "same order", "employee": "processed by"}
_NODE_STYLE = {
    "current": ('"#0f766e"', '"#2dd4bf"', 3),
    "duplicate_id": ('"#7f1d1d"', '"#f87171"', 2),
    "same_order": ('"#78350f"', '"#fbbf24"', 2),
    "same_account": ('"#5b3a0a"', '"#f59e0b"', 2),
    "other": ('"#1f2d44"', '"#64748b"', 1),
}


def graph_to_dot(graph: dict) -> str:
    """Graphviz DOT text (dark-theme friendly) for st.graphviz_chart."""
    ids = {n["id"]: f"n{i}" for i, n in enumerate(graph["nodes"])}

    def esc(text: Any) -> str:
        return str(text).replace("\\", "\\\\").replace('"', "'")

    lines = [
        "graph G {",
        'rankdir=LR; bgcolor="transparent"; pad=0.2; nodesep=0.35; ranksep=0.9;',
        'node [fontname="Helvetica", fontsize=12, fontcolor="#e8eef8", margin="0.16,0.09"];',
        'edge [color="#7f93ad", fontname="Helvetica", fontsize=10, fontcolor="#aab9cf"];',
    ]
    for n in graph["nodes"]:
        nid = ids[n["id"]]
        kind = n["kind"]
        if kind == "transaction":
            fill, line, width = _NODE_STYLE.get(n.get("relation", "other"), _NODE_STYLE["other"])
            tag = {"current": "THIS REQUEST", "duplicate_id": "SAME ID", "same_order": "SAME ORDER",
                   "same_account": "SAME ACCOUNT"}.get(n.get("relation"), "")
            label = f"{esc(n['label'])}\\n{_inr(n.get('amount', 0))}" + (f"\\n{tag}" if tag else "")
            lines.append(f'{nid} [label="{label}", shape=box, style="filled,rounded", fillcolor={fill}, color={line}, penwidth={width}];')
        elif kind == "account":
            lines.append(f'{nid} [label="Account\\n{esc(n["label"])}", shape=ellipse, style=filled, fillcolor="#1e3a5f", color="#60a5fa"];')
        elif kind == "order":
            lines.append(f'{nid} [label="Order\\n{esc(n["label"])}", shape=hexagon, style=filled, fillcolor="#2e2a6b", color="#a78bfa"];')
        else:
            lines.append(f'{nid} [label="Employee\\n{esc(n["label"])}", shape=box, style="dashed,filled", fillcolor="#162033", color="#94a3b8"];')
    for e in graph["edges"]:
        lines.append(f'{ids[e["source"]]} -- {ids[e["target"]]} [label="{_EDGE_LABEL.get(e["kind"], "")}"];')
    lines.append("}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Investigation tools. Each takes (tx, ctx) and returns a finding dict.
# ctx = {"policy", "history", "approvals", "memory"}
# --------------------------------------------------------------------------- #
def tool_validate_completeness(tx: dict, ctx: dict) -> dict:
    labels = {"account_ref": "account reference", "original_transaction_id": "original order ID",
              "employee_id": "employee ID"}
    missing = [label for key, label in labels.items() if not tx.get(key)]
    if missing:
        return _finding("validate_completeness", "FLAG",
                        f"The request is missing: {', '.join(missing)}. Related records cannot be checked reliably without it.",
                        {"missing_fields": missing}, 15)
    return _finding("validate_completeness", "PASS", "All required details are present.",
                    {"fields_checked": list(labels.values())})


def tool_verify_role(tx: dict, ctx: dict) -> dict:
    allowed = ctx["policy"]["allowed_roles"]
    role = tx["employee_role"]
    ok = role in allowed
    summary = (f"Role '{_role_label(role)}' is allowed to process refunds." if ok else
               f"Role '{_role_label(role)}' is NOT allowed to process refunds "
               f"(allowed: {', '.join(_role_label(r) for r in allowed)}).")
    return _finding("verify_employee_role", "PASS" if ok else "VIOLATION", summary,
                    {"employee_role": role, "allowed_roles": allowed, "authorized": ok}, 0 if ok else 70)


def tool_retrieve_policy(tx: dict, ctx: dict) -> dict:
    p = ctx["policy"]
    rules = [
        f"Refunds above {_inr(p['refund_limit'])} need documented approval from: "
        f"{', '.join(_role_label(r) for r in p['approver_roles'])}.",
        f"Only these roles may process refunds: {', '.join(_role_label(r) for r in p['allowed_roles'])}.",
        "A transaction ID that was already processed must never be processed again.",
        f"{p['repeat_refund_threshold']}+ refunds on one account within {p['history_window_days']} days are treated as repeated activity.",
    ]
    return _finding("retrieve_policy", "INFO", "Retrieved the policy rules that apply to this case.",
                    {"applicable_rules": rules, "refund_limit": p["refund_limit"]})


def tool_check_refund_limit(tx: dict, ctx: dict) -> dict:
    limit = float(ctx["policy"]["refund_limit"])
    amount = tx["amount"]
    if amount <= limit:
        return _finding("check_refund_limit", "PASS",
                        f"{_inr(amount)} is within the {_inr(limit)} limit.",
                        {"refund_amount": amount, "refund_limit": limit, "within_limit": True})
    return _finding("check_refund_limit", "FLAG",
                    f"{_inr(amount)} is {_inr(amount - limit)} above the {_inr(limit)} limit, so documented approval is required.",
                    {"refund_amount": amount, "refund_limit": limit, "within_limit": False,
                     "excess_amount": round(amount - limit, 2)}, 20)


def tool_check_approval(tx: dict, ctx: dict) -> dict:
    p = ctx["policy"]
    amount, ref = tx["amount"], tx.get("approval_ref", "")
    base = {"approval_ref": ref or "(none supplied)", "requested_amount": amount}
    if amount <= float(p["refund_limit"]):
        return _finding("check_approval_evidence", "PASS", "No approval is needed for this amount.", base)
    if not ref:
        return _finding("check_approval_evidence", "FLAG",
                        "No approval reference was supplied, but this amount requires one.", base, 20)
    rec = ctx["approvals"].get(ref)
    if not rec:
        return _finding("check_approval_evidence", "FLAG",
                        f"Approval '{ref}' does not exist in the approval register.", {**base, "found": False}, 20)
    ev = {**base, "found": True, "approver_role": rec["approver_role"], "covers_up_to": rec["max_amount"],
          "still_valid": bool(rec["valid"])}
    if not rec["valid"]:
        return _finding("check_approval_evidence", "FLAG", f"Approval '{ref}' exists but has been revoked or expired.", ev, 20)
    if rec["approver_role"] not in p["approver_roles"]:
        return _finding("check_approval_evidence", "FLAG",
                        f"Approval '{ref}' was issued by a '{_role_label(rec['approver_role'])}', who may not approve refunds.", ev, 20)
    if float(rec["max_amount"]) < amount:
        return _finding("check_approval_evidence", "FLAG",
                        f"Approval '{ref}' only covers up to {_inr(rec['max_amount'])}, less than the requested {_inr(amount)}.", ev, 20)
    return _finding("check_approval_evidence", "PASS",
                    f"Approval '{ref}' from a {_role_label(rec['approver_role'])} is valid and covers up to {_inr(rec['max_amount'])}.",
                    ev, 0, resolves=["check_refund_limit"])


def tool_detect_duplicates(tx: dict, ctx: dict) -> dict:
    hist = ctx["history"]
    same_id = [h for h in hist if _eq(h.get("transaction_id"), tx["transaction_id"])]
    same_order = [h for h in hist if _eq(h.get("original_transaction_id"), tx["original_transaction_id"])
                  and not _eq(h.get("transaction_id"), tx["transaction_id"])]
    ev = {"same_transaction_id": len(same_id), "same_order_refunds": len(same_order),
          "matches": (same_id + same_order)[:6]}
    if same_id:
        return _finding("detect_duplicate_transaction", "VIOLATION",
                        f"Transaction ID {tx['transaction_id']} was already processed. A transaction ID can only be used once.",
                        ev, 70)
    if same_order:
        total = sum(float(h.get("amount") or 0) for h in same_order)
        return _finding("detect_duplicate_transaction", "FLAG",
                        f"Order {tx['original_transaction_id']} has already been refunded {len(same_order)} time(s) "
                        f"({_inr(total)} in total). This may be a repeated refund.", ev, 30)
    return _finding("detect_duplicate_transaction", "PASS",
                    "No earlier record uses this transaction ID or refunds this order.", ev)


def tool_inspect_history(tx: dict, ctx: dict) -> dict:
    p = ctx["policy"]
    window = int(p["history_window_days"])
    if not tx.get("account_ref"):
        return _finding("inspect_transaction_history", "INFO",
                        "Skipped: no account reference, so history could not be searched.", {})
    related = [h for h in ctx["history"] if _eq(h.get("account_ref"), tx["account_ref"])
               and not _eq(h.get("transaction_id"), tx["transaction_id"])
               and int(h.get("days_ago") or 0) <= window]
    refunds = [h for h in related if str(h.get("type", "refund")).lower() == "refund"]
    total = sum(float(h.get("amount") or 0) for h in refunds)
    ev = {"window_days": window, "refunds_in_window": len(refunds), "total_refunded": total,
          "records": related[:8]}
    if len(refunds) >= int(p["repeat_refund_threshold"]):
        pts = 30 if len(refunds) >= 4 else 20
        return _finding("inspect_transaction_history", "FLAG",
                        f"Account {tx['account_ref']} already had {len(refunds)} refunds ({_inr(total)}) in the last {window} days. "
                        "This request would add to that pattern.", ev, pts)
    return _finding("inspect_transaction_history", "PASS",
                    f"Account {tx['account_ref']} has {len(refunds)} refund(s) in the last {window} days, which is normal.", ev)


def tool_analyze_graph(tx: dict, ctx: dict) -> dict:
    g = build_graph_data(tx, ctx["history"])
    linked = g["linked_transactions"]
    size = linked + 1
    ev = {"linked_transactions": linked,
          "accounts_involved": sorted(n["label"] for n in g["nodes"] if n["kind"] == "account"),
          "orders_involved": sorted(n["label"] for n in g["nodes"] if n["kind"] == "order"),
          "employees_involved": sorted(n["label"] for n in g["nodes"] if n["kind"] == "employee")}
    if size >= int(ctx["policy"]["cluster_flag_size"]):
        return _finding("analyze_relationship_graph", "FLAG",
                        f"This request is connected to {linked} other transactions through a shared account or order. "
                        "Connections are a lead to check, not proof of fraud.", ev, 15)
    return _finding("analyze_relationship_graph", "PASS",
                    f"Only {linked} other transaction(s) are connected to this request; no unusual cluster.", ev)


def tool_find_similar_cases(tx: dict, ctx: dict) -> dict:
    matches = [m for m in ctx["memory"]
               if _eq(m.get("account_ref"), tx["account_ref"]) or
               _eq(m.get("original_transaction_id"), tx["original_transaction_id"])]
    rows = [{"case_id": m["case_id"], "transaction_id": m["transaction_id"], "decision": m["decision"],
             "outcome": m.get("case_outcome", "OPEN"), "date": str(m.get("created_at", ""))[:10]} for m in matches[:5]]
    if not rows:
        return _finding("find_similar_cases", "INFO", "No earlier investigations involve this account or order.",
                        {"similar_cases": []})
    return _finding("find_similar_cases", "INFO",
                    f"Found {len(matches)} earlier investigation(s) on the same account or order. "
                    "Their outcomes are background only. They are not evidence about this transaction.",
                    {"similar_cases": rows})


TOOL_CATALOG: dict[str, dict[str, Any]] = {
    "validate_completeness": {"title": "Is the request complete?", "fn": tool_validate_completeness},
    "verify_employee_role": {"title": "Is this employee allowed to process refunds?", "fn": tool_verify_role},
    "retrieve_policy": {"title": "Which policy rules apply?", "fn": tool_retrieve_policy},
    "check_refund_limit": {"title": "Is the amount within the refund limit?", "fn": tool_check_refund_limit},
    "check_approval_evidence": {"title": "Is there valid approval for this amount?", "fn": tool_check_approval},
    "detect_duplicate_transaction": {"title": "Has this transaction or order been processed before?", "fn": tool_detect_duplicates},
    "inspect_transaction_history": {"title": "Is there unusual refund activity on this account?", "fn": tool_inspect_history},
    "analyze_relationship_graph": {"title": "How is this request connected to other records?", "fn": tool_analyze_graph},
    "find_similar_cases": {"title": "Have we investigated something similar before?", "fn": tool_find_similar_cases},
}

INITIAL_PLAN = [
    ("validate_completeness", "Every case starts by checking that the request has the details needed for the other checks."),
    ("verify_employee_role", "A mandatory rule: only authorised roles may process refunds."),
    ("check_refund_limit", "A mandatory rule: large refunds need documented approval."),
    ("detect_duplicate_transaction", "Repeated or re-used transactions are a common source of loss."),
    ("inspect_transaction_history", "Many small refunds on one account can add up to a pattern that no single request shows."),
]


# --------------------------------------------------------------------------- #
# Agent loop: re-assessment after every step
# --------------------------------------------------------------------------- #
def _reassess(finding: dict, done: set[str], queued: list[str]) -> tuple[list[tuple[str, str]], bool]:
    """Decide what to do after a tool result. Returns (follow_ups, stop_investigating)."""
    tool, status = finding["tool"], finding["status"]
    adds: list[tuple[str, str]] = []
    if status == "VIOLATION":
        if "retrieve_policy" not in done:
            adds.append(("retrieve_policy", "A mandatory rule was broken. Fetch the exact rule to cite in the report."))
        return adds, True
    if tool == "check_refund_limit" and status == "FLAG":
        adds += [("retrieve_policy", "The amount is over the limit. Fetch the exact approval rule."),
                 ("check_approval_evidence", "Check whether valid approval for this amount was supplied.")]
    if tool == "inspect_transaction_history" and status == "FLAG":
        adds += [("analyze_relationship_graph", "Repeated activity found. Map how these records connect.")]
    if tool == "detect_duplicate_transaction" and status == "FLAG" and not any(
            n in done or n in queued for n in ("analyze_relationship_graph",)):
        adds += [("analyze_relationship_graph", "A related refund exists. Map how the records connect.")]
    adds = [(n, w) for n, w in adds if n not in done and n not in queued]
    seen, unique = set(), []
    for n, w in adds:
        if n not in seen:
            seen.add(n)
            unique.append((n, w))
    return unique, False


def investigate_transaction(
    transaction: dict,
    history: list[dict] | None = None,
    policy: dict | None = None,
    approvals: dict | None = None,
    memory: list[dict] | None = None,
    proposer: Callable[[dict], list] | None = None,
) -> dict:
    """Run a full investigation and return a case report.

    `proposer` is an optional hook for a language-model planner. It may suggest extra tools
    after each step; every suggestion is checked against TOOL_CATALOG and unknown tools are
    rejected and logged. Mandatory decisions are always made by decide().
    """
    tx = normalize_transaction(transaction)
    policy = {**DEFAULT_POLICY, **(policy or {})}
    ctx = {"policy": policy, "history": list(history or []), "approvals": dict(approvals or {}),
           "memory": list(memory or [])}
    case_id = f"CS-{uuid.uuid4().hex[:8].upper()}"
    started = _now()

    audit: list[dict] = []

    def log(event: str, detail: str) -> None:
        audit.append({"seq": len(audit) + 1, "timestamp": _now(), "event": event, "detail": detail})

    log("case_received", f"Refund request {tx['transaction_id']} for {_inr(tx['amount'])} received "
                         f"from {_role_label(tx['employee_role'])} {tx['employee_id'] or '(no ID)'}.")

    queue: list[tuple[str, str]] = list(INITIAL_PLAN)
    steps: list[dict] = []
    done: set[str] = set()
    stopped_early = ""

    while queue and len(steps) < MAX_STEPS:
        name, why = queue.pop(0)
        if name in done:
            continue
        spec = TOOL_CATALOG[name]
        log("tool_selected", f"{spec['title']} Reason: {why}")
        try:
            finding = spec["fn"](tx, ctx)
        except Exception as exc:  # a failing tool must never crash the case or be hidden
            finding = _finding(name, "ERROR", f"The check could not run ({type(exc).__name__}). Treated as missing evidence.",
                               {"error": str(exc)}, 25)
        done.add(name)
        finding.update(n=len(steps) + 1, title=spec["title"], why=why)
        log("tool_result", f"{name}: {finding['status']}. {finding['summary']}")

        adds, stop = _reassess(finding, done, [q[0] for q in queue])
        if stop:
            queue = [(n, w) for n, w in adds]
            stopped_early = ("A mandatory rule was broken, so no further investigation can change the outcome. "
                             "The agent stopped early.")
        else:
            queue = adds + queue
            if proposer:
                try:
                    proposals = proposer({"transaction": dict(tx), "available_tools": list(TOOL_CATALOG),
                                          "steps": [{"tool": s["tool"], "status": s["status"], "summary": s["summary"]}
                                                    for s in steps + [finding]]}) or []
                except Exception as exc:
                    proposals = []
                    log("planner_error", f"The AI planner failed ({type(exc).__name__}); continuing with built-in planning.")
                for p in proposals:
                    pname = p.get("tool") if isinstance(p, dict) else p
                    if pname not in TOOL_CATALOG:
                        log("tool_call_rejected", f"The AI planner asked for unknown tool '{pname}'. Rejected: not in the approved tool list.")
                    elif pname not in done and pname not in [q[0] for q in queue]:
                        queue.append((pname, "Suggested by the AI planner and approved against the tool list."))
                        adds.append((pname, "Suggested by the AI planner."))

        if not queue and not stopped_early and "find_similar_cases" not in done and any(
                s["status"] in ("FLAG", "ERROR") for s in steps + [finding]):
            queue.append(("find_similar_cases", "Something suspicious was found. Look for similar earlier cases for context."))
            adds.append(queue[-1])

        if adds:
            finding["next"] = "Added: " + "; ".join(f"{TOOL_CATALOG[n]['title']}" for n, _ in adds)
        elif stopped_early:
            finding["next"] = "Stopped early (mandatory rule broken)."
        elif queue:
            finding["next"] = "Continue with: " + TOOL_CATALOG[queue[0][0]]["title"]
        else:
            finding["next"] = "No further checks needed. Ready to decide."
        steps.append(finding)

    # Findings resolved by a later passing check (e.g. valid approval resolves the limit flag)
    for f in steps:
        if f["status"] == "PASS":
            for target in f.get("resolves", []):
                for g in steps:
                    if g["tool"] == target and g["status"] == "FLAG":
                        g["resolved_by"] = f["tool"]

    report = decide(tx, steps, policy)
    report.update({
        "case_id": case_id,
        "transaction_id": tx["transaction_id"],
        "started_at": started,
        "transaction": tx,
        "steps": steps,
        "tool_results": steps,                       # alias kept for compatibility
        "plan": [s["tool"] for s in steps],
        "stopped_early": stopped_early,
        "graph": build_graph_data(tx, ctx["history"]),
        "policy_snapshot": {k: policy[k] for k in ("refund_limit", "allowed_roles", "approver_roles")},
        "action_status": "NOT_EXECUTED",
        "verification_status": "NOT_APPLICABLE",
        "verified": False,
        "case_outcome": "OPEN",
        "before_status": "PENDING",
        "after_status": "PENDING",
        "approval": None,
        "last_refusal": "",
        "ai_summary": None,
    })
    log("decision_made", f"Decision {report['decision']} with risk score {report['risk_score']}/100. "
                         f"Recommended action: {ACTIONS[report['recommended_action_key']]['label']}.")
    report["audit_events"] = audit
    report["narrative"] = build_narrative(report)
    return report


# --------------------------------------------------------------------------- #
# Decision engine (deterministic)
# --------------------------------------------------------------------------- #
def decide(tx: dict, steps: list[dict], policy: dict) -> dict:
    active = [s for s in steps if not s.get("resolved_by")]
    violations = [s for s in active if s["status"] == "VIOLATION"]
    flags = [s for s in active if s["status"] in ("FLAG", "ERROR")]

    if violations:
        decision = "BLOCK"
        reasons = [s["summary"] for s in violations] + [s["summary"] for s in flags]
    elif flags:
        decision = "ESCALATE"
        reasons = [s["summary"] for s in flags]
    else:
        decision = "ALLOW"
        passed = [s for s in steps if s["status"] == "PASS"]
        reasons = [f"All {len(passed)} checks passed. No policy problem was found."]
        for s in steps:
            if s.get("resolved_by"):
                reasons.append("The amount is above the limit, but a valid approval covers it.")

    breakdown = []
    for s in steps:
        if s["risk_points"] or s.get("resolved_by"):
            breakdown.append({"tool": s["tool"], "label": s["title"],
                              "points": 0 if s.get("resolved_by") else s["risk_points"],
                              "status": s["status"], "resolved": bool(s.get("resolved_by"))})
    score = min(100, sum(b["points"] for b in breakdown))
    band = "HIGH" if score >= 60 else "MEDIUM" if score >= 25 else "LOW"

    missing: list[str] = []
    for s in steps:
        if s["tool"] == "validate_completeness" and s["status"] == "FLAG":
            missing += [f"Missing {m}" for m in s["evidence"]["missing_fields"]]
        if s["tool"] == "check_approval_evidence" and s["status"] == "FLAG":
            missing.append("Valid approval evidence for this amount")
        if s["status"] == "ERROR":
            missing.append(f"Result of '{s['tool']}' (the check failed)")

    if decision == "BLOCK":
        confidence = {"level": "High", "why": "Based on a mandatory rule that was checked directly."}
    elif decision == "ALLOW":
        confidence = {"level": "High" if not missing else "Medium",
                      "why": "All configured checks passed on complete data. Only configured checks were run."}
    elif any(s["status"] == "ERROR" for s in steps) or missing:
        confidence = {"level": "Low", "why": "Some evidence is missing or a check failed, so a human must decide."}
    else:
        confidence = {"level": "Medium", "why": "Indicators were found, but indicators are not proof of wrongdoing."}

    action_key = RECOMMENDED_ACTION[decision]
    return {
        "decision": decision,
        "reasons": reasons,
        "risk_score": score,
        "risk_band": band,
        "score_breakdown": breakdown,
        "missing_information": missing,
        "confidence": confidence,
        "recommended_action_key": action_key,
        "recommended_action": ACTIONS[action_key]["label"],
        "allowed_actions": ALLOWED_ACTIONS[decision],
        "requires_human_approval": decision in ("BLOCK", "ESCALATE"),
        "investigation_complete": True,
    }


def build_narrative(report: dict) -> dict:
    tx = report["transaction"]
    who = _role_label(tx["employee_role"]).capitalize() + (f" {tx['employee_id']}" if tx.get("employee_id") else "")
    where = ", ".join(x for x in [
        f"transaction {tx['transaction_id']}",
        f"order {tx['original_transaction_id']}" if tx.get("original_transaction_id") else "",
        f"account {tx['account_ref']}" if tx.get("account_ref") else ""] if x)
    headline = {
        "ALLOW": "Cleared: no policy problems were found.",
        "ESCALATE": "Needs human review: suspicious signals were found, but nothing is proven.",
        "BLOCK": "Blocked: a mandatory policy rule was broken.",
    }[report["decision"]]
    nxt = f"Recommended next step: {report['recommended_action'].lower()}."
    if report["decision"] == "ESCALATE":
        nxt += " An authorised reviewer should look at the evidence. This is not a fraud accusation."
    elif report["decision"] == "BLOCK":
        nxt += " Policy does not allow the model or a reviewer to release this request."
    return {"headline": headline,
            "what_happened": f"{who} asked to refund {_inr(tx['amount'])} ({where}).",
            "checked": f"The agent ran {len(report['steps'])} checks: "
                       + ", ".join(s["title"].rstrip("?").lower() for s in report["steps"]) + ".",
            "next_step": nxt}


# --------------------------------------------------------------------------- #
# Guarded actions + closed-loop verification
# --------------------------------------------------------------------------- #
def needs_approval(report: dict, action: str) -> bool:
    """Only releasing a fully cleared case is allowed without a human reviewer."""
    return not (report["decision"] == "ALLOW" and action == "allow")


def execute_action(report: dict, action: str, ledger, approver: dict | None = None,
                   note: str = "", simulate_failure: bool = False) -> dict:
    """Try to apply an action. Never raises on a policy refusal; the refusal is recorded instead.

    ledger must provide read_status(case_id) and write_status(case_id, tx_id, status, simulate_failure).
    """
    out = deepcopy(report)
    events = out["audit_events"]

    def log(event: str, detail: str) -> None:
        events.append({"seq": len(events) + 1, "timestamp": _now(), "event": event, "detail": detail})

    def refuse(reason: str) -> dict:
        out["last_refusal"] = reason
        log("action_refused", f"Action '{action}' was refused: {reason}")
        return out

    out["last_refusal"] = ""
    if out.get("action_status") == "EXECUTED":
        return refuse("An action has already been executed for this case.")
    if action not in ACTIONS:
        return refuse(f"'{action}' is not a known action.")
    if action not in out["allowed_actions"]:
        return refuse(f"Policy does not allow '{ACTIONS[action]['label']}' for a {out['decision']} case.")

    approval = None
    if needs_approval(out, action):
        approver = approver or {}
        name, role = str(approver.get("name", "")).strip(), str(approver.get("role", "")).strip()
        if not name or not role:
            return refuse("A named reviewer with a role must approve this action.")
        if role not in out["policy_snapshot"]["approver_roles"]:
            return refuse(f"Role '{_role_label(role)}' is not authorised to approve actions.")
        if name.lower() == str(out["transaction"].get("employee_id", "")).lower():
            return refuse("The person who raised the request cannot approve it (separation of duties).")
        if not note.strip():
            return refuse("A short reason is required so the decision can be audited.")
        approval = {"name": name, "role": role, "note": note.strip(), "timestamp": _now()}
        out["approval"] = approval
        log("human_approval", f"{name} ({_role_label(role)}) approved '{action}'. Reason: {note.strip()}")
    else:
        log("auto_approved_by_policy", "The case was cleared by every check, so policy allows release without a reviewer.")

    expected = ACTIONS[action]["status"]
    case_id, tx_id = out["case_id"], out["transaction_id"]
    before = ledger.read_status(case_id) or "PENDING"
    out["before_status"], out["expected_status"], out["action_taken"] = before, expected, action
    ledger.write_status(case_id, tx_id, expected, simulate_failure=simulate_failure)
    out["action_status"] = "EXECUTED"
    log("action_executed", f"Requested status change {before} to {expected}.")

    actual = ledger.read_status(case_id) or "PENDING"     # re-read the real state; do not trust the write
    ok = actual == expected
    out["after_status"] = actual
    out["transaction"]["status"] = actual
    out["verified"] = ok
    out["verification_status"] = "VERIFIED" if ok else "FAILED"
    out["case_outcome"] = "RESOLVED" if ok else "UNRESOLVED"
    log("outcome_verification", f"Expected {expected}; the ledger shows {actual}. "
        + ("Verified. Case resolved." if ok else "MISMATCH. The case stays open and is escalated for manual follow-up."))
    return out


# --------------------------------------------------------------------------- #
# Report export
# --------------------------------------------------------------------------- #
def report_to_markdown(report: dict) -> str:
    n, tx = report["narrative"], report["transaction"]
    lines = [
        f"# ChainShield AI case report: {report['case_id']}", "",
        f"**Decision:** {report['decision']}  ",
        f"**Risk score:** {report['risk_score']}/100 ({report['risk_band']})  ",
        f"**Confidence:** {report['confidence']['level']} ({report['confidence']['why']})  ",
        f"**Outcome:** {report.get('case_outcome', 'OPEN')}", "",
        "## Summary", n["headline"], "", n["what_happened"], "", n["next_step"], "",
        "## Why", *[f"- {r}" for r in report["reasons"]], "",
        "## Investigation steps",
    ]
    for s in report["steps"]:
        lines.append(f"{s['n']}. **{s['title']}** [{s['status']}] {s['summary']}  \n   _Why the agent ran it:_ {s['why']}")
    if report["missing_information"]:
        lines += ["", "## Missing information", *[f"- {m}" for m in report["missing_information"]]]
    lines += ["", "## Action and verification",
              f"- Action: {report.get('action_status')}", f"- Verification: {report.get('verification_status')}",
              f"- Status: {report.get('before_status')} to {report.get('after_status')}"]
    if report.get("approval"):
        a = report["approval"]
        lines.append(f"- Approved by: {a['name']} ({_role_label(a['role'])}). Reason: {a['note']}")
    lines += ["", "## Audit timeline", *[f"- {e['timestamp']} · {e['event']} · {e['detail']}" for e in report["audit_events"]]]
    lines += ["", f"_Transaction {tx['transaction_id']}. Synthetic data; prototype only._"]
    return "\n".join(lines)
