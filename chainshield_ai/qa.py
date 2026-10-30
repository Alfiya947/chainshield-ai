"""Grounded Q&A about a finished case. Used by both typed and spoken questions.

Safety design:
  * Answers are built ONLY from the case report (no outside facts, nothing invented).
  * Requests to ACT ("approve it", "ignore the rules", "go ahead") are always refused here,
    before any language model is consulted. Actions only happen in the approval gate.
  * "Is this fraud?" is answered with what is and is not proven; the system never declares fraud.
  * An optional LLM may rephrase non-sensitive answers; if it fails we use the deterministic answer.
  * Every question is recorded in the case's audit trail. The decision is never modified.
"""
from __future__ import annotations

import re
from copy import deepcopy
from datetime import datetime, timezone
from typing import Callable

MAX_QUESTION_CHARS = 300

SUGGESTED = ["Why was this flagged?", "What is missing?", "What do you recommend?",
             "How risky is it?", "What did you check?", "How is it connected to other records?"]

_INTERROGATIVE = re.compile(r"^(should|shall|would|what|why|how|is|are|does|do|which|who|when|where|can i|could i)\b")
_STRONG_ACTION = re.compile(r"\b(go ahead|do it|just do|ignore (all |the |your |previous )*(instructions|rules|policy|checks)|"
                            r"override|bypass|skip (the )?(approval|checks?|review)|without approval)\b")
_IMPERATIVE = re.compile(r"^(please |kindly )?(can you |could you |would you |go ahead and |just )?"
                         r"(approve|execute|release|reject|authori[sz]e|process|mark|place|block|allow|refund|pay|confirm|clear)\b")

ACTION_REFUSAL = ("I can't take actions by voice or chat. Approving, rejecting or releasing a request needs a named, "
                  "authorised reviewer with a written reason, in the Approve and verify section. "
                  "I can explain the case so you can decide.")


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]", " ", text.lower())).strip()


def _step(r: dict, tool: str) -> dict | None:
    return next((s for s in r["steps"] if s["tool"] == tool), None)


def _sentences(items: list[str], limit: int = 3) -> str:
    items = [i.rstrip(".") + "." for i in items]
    extra = len(items) - limit
    out = " ".join(items[:limit])
    return out + (f" There {'is' if extra == 1 else 'are'} {extra} more in the report." if extra > 0 else "")


def detect_intent(question: str) -> str:
    q = _norm(question)
    if not q:
        return "empty"
    if _STRONG_ACTION.search(q) or (not _INTERROGATIVE.match(q) and _IMPERATIVE.match(q)) or q.startswith(("can you approve", "can you reject", "can you release")):
        return "action"
    rules = [
        ("fraud", r"\b(fraud|fraudulent|guilty|criminal|stole|steal|scam|theft|cheat\w*)\b"),
        ("missing", r"\b(missing|incomplete|unknown|don't know|do not know|lack\w*|not sure about|gaps?)\b"),
        ("recommend", r"\b(recommend\w*|should i|what should|next step|what do i do|suggest\w*|what now)\b"),
        ("score", r"\b(score|how risky|risk level|risk band|how risk)\b"),
        ("confidence", r"\b(confiden\w*|how sure|how certain|certainty)\b"),
        ("approval", r"\b(approval|approved|approver)\b"),
        ("connections", r"\b(connect\w*|linked|related|graph|relationship\w*|pattern|same account|same order|other records)\b"),
        ("similar", r"\b(similar|earlier|before|past cases?|previous cases?|history|memory)\b"),
        ("checks", r"\b(checks?|checked|steps?|tools?|investigat\w*|process|how did you)\b"),
        ("summary", r"\b(summar\w*|what happened|brief\w*|overview|tell me about|explain the case)\b"),
        ("why", r"\b(why|reason\w*|flag\w*|suspicious|wrong|problem|issue|explain|blocked|escalat\w*)\b"),
    ]
    for name, pattern in rules:
        if re.search(pattern, q):
            return name
    return "unknown"


def _answer_for(intent: str, r: dict) -> str:
    n, tx, dec = r["narrative"], r["transaction"], r["decision"]
    if intent == "empty":
        return "Please type or say a question about this case."
    if intent == "action":
        return ACTION_REFUSAL
    if intent == "fraud":
        return ("Nothing here proves fraud. The system only finds indicators and policy problems for a human to review; it never "
                f"declares fraud. For this case the result is {dec.lower().replace('escalate', 'needs review')}. "
                + ("The findings are: " + _sentences(r["reasons"], 2) if dec != "ALLOW" else "No policy problems were found."))
    if intent == "summary":
        return f"{n['headline']} {n['what_happened']} Risk score {r['risk_score']} out of 100."
    if intent == "why":
        if dec == "ALLOW":
            return "Nothing was flagged. " + r["reasons"][0]
        lead = "It was blocked because " if dec == "BLOCK" else "It needs review because "
        return lead + "of the following. " + _sentences(r["reasons"])
    if intent == "missing":
        if r["missing_information"]:
            return "These pieces of evidence are missing: " + "; ".join(r["missing_information"]) + "."
        return ("Nothing essential is missing for the checks that were run. Only the configured checks were performed, "
                "and all records are synthetic.")
    if intent == "recommend":
        gate = (" A named, authorised reviewer must approve it in the Approve and verify section."
                if r["requires_human_approval"] else " Policy allows this without a reviewer.")
        return f"I recommend: {r['recommended_action'].lower()}.{gate}"
    if intent == "score":
        top = sorted((b for b in r["score_breakdown"] if b["points"]), key=lambda b: -b["points"])[:2]
        why = (" The biggest contributors are: " + "; ".join(f"{b['label'].rstrip('?').lower()}, {b['points']} points" for b in top) + "."
               if top else " No risk points were added.")
        return (f"The risk score is {r['risk_score']} out of 100, which is {r['risk_band'].lower()}." + why +
                " It is a triage priority, not a fraud probability.")
    if intent == "confidence":
        return f"Confidence is {r['confidence']['level'].lower()}. {r['confidence']['why']}"
    if intent == "approval":
        s = _step(r, "check_approval_evidence")
        if s:
            return s["summary"]
        lim = _step(r, "check_refund_limit")
        return ("No approval check was needed. " + lim["summary"]) if lim else "No approval check was run for this case."
    if intent == "connections":
        g = _step(r, "analyze_relationship_graph")
        if g and g["status"] == "FLAG":
            return g["summary"]
        return (f"This request is linked to {r['graph']['linked_transactions']} other transaction(s) through a shared account "
                "or order. Links are leads for a reviewer, not proof of wrongdoing.")
    if intent == "similar":
        s = _step(r, "find_similar_cases")
        return s["summary"] if s else ("No search for similar earlier cases was run, because nothing suspicious was found.")
    if intent == "checks":
        titles = "; ".join(s["title"].rstrip("?").lower() for s in r["steps"])
        return (f"The agent ran {len(r['steps'])} checks: {titles}." +
                (" It stopped early because a mandatory rule was broken." if r.get("stopped_early") else ""))
    return ("I can answer questions about this case. Try: why was it flagged, what is missing, what do you recommend, "
            "how risky is it, what did you check, or how is it connected to other records.")


def answer_question(report: dict, question: str, llm: Callable[[dict, str], str | None] | None = None) -> dict:
    """Return {"question", "answer", "intent", "source"}. source is 'rules' or 'llm'."""
    question = (question or "").strip()[:MAX_QUESTION_CHARS]
    intent = detect_intent(question)
    answer, source = _answer_for(intent, report), "rules"
    if llm and intent not in ("action", "fraud", "empty", "unknown"):   # sensitive intents stay deterministic
        try:
            text = llm(report, question)
        except Exception:
            text = None
        if text:
            answer, source = text.strip(), "llm"
    return {"question": question, "answer": answer, "intent": intent, "source": source}


def log_question(report: dict, qa: dict, via: str = "typed") -> dict:
    """Return a copy of the report with the question recorded in the audit trail. Decision fields are untouched."""
    out = deepcopy(report)
    events = out["audit_events"]
    detail = f"Reviewer asked ({via}): \"{qa['question']}\". Intent: {qa['intent']}. Answered from case facts ({qa['source']})."
    if qa["intent"] == "action":
        detail += " Request to act was refused; actions require the approval gate."
    events.append({"seq": len(events) + 1, "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "event": "question_answered", "detail": detail})
    return out
