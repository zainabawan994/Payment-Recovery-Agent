"""LangGraph state machine: analyze failure -> generate recovery plan -> execute recovery."""
import operator
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

import memory
import tools
from classifier import classify

# Recovery strategy per failure category. Retrying the same card only makes sense for transient errors;
# everything else needs the customer to act, so they get a fresh checkout link.
POLICY = {
    "processing_error": ["retry", "notify_link"],
    "insufficient_funds": ["notify_link"],
    "expired_card": ["notify_link"],
    "incorrect_details": ["notify_link"],
    "authentication_required": ["notify_link"],  # 3DS needs the customer present
    "generic_decline": ["notify_link"],
    "fraud_suspected": ["manual_review"],
}
MAX_PRIOR_FAILURES = 3  # same customer failing this often goes to a human


class State(TypedDict, total=False):
    failure: dict  # id, payment_intent_id, payment_method_id, customer_id, phone, amount, currency, decline_code, message, card, risk_level
    history: list[dict]
    classification: dict
    card_check: dict
    plan: list[str]
    status: str
    logs: Annotated[list[str], operator.add]


def analyze(state: State) -> State:
    f = state["failure"]
    history = memory.recall(f.get("customer_id"), f"{f.get('decline_code')} {f.get('message')}", f["id"])
    c = classify({k: v for k, v in f.items() if k not in ("phone", "id")}, history)
    card = tools.bin_check(f.get("card") or {}, f.get("risk_level"))
    return {"history": history, "classification": c.model_dump(), "card_check": card,
            "logs": [f"classified {c.category}: {c.root_cause}", f"card check {card}"]}


def plan(state: State) -> State:
    f, card = state["failure"], state["card_check"]
    category = state["classification"]["category"]
    prior = sum(1 for h in state["history"] if f.get("customer_id") and h["customer_id"] == f["customer_id"])
    if card["risky"] or prior >= MAX_PRIOR_FAILURES:
        steps, why = ["manual_review"], f"risky={card['risky']} prior_failures={prior}"
    else:
        steps = [s for s in POLICY[category] if not (s == "retry" and (not card["valid"] or not f.get("payment_method_id")))]
        why = f"policy for {category}"
    return {"plan": steps, "logs": [f"plan {steps} ({why})"]}


def execute(state: State) -> State:
    f, logs, status = state["failure"], [], "manual_review"
    for step in state["plan"]:
        if step == "retry":
            if tools.retry_payment(f["payment_intent_id"], f["payment_method_id"]):
                logs.append("retry succeeded")
                status = "recovered"
                break
            logs.append("retry failed")
        elif step == "notify_link":
            link = tools.create_checkout_link(f["amount"], f["currency"], f.get("customer_id"), f["id"])
            logs.append(f"checkout link {link}")
            if f.get("phone"):
                msg_id = tools.send_whatsapp(f["phone"], f"{f['amount'] / 100:.2f} {f['currency'].upper()}", link)
                logs.append(f"whatsapp sent {msg_id}")
                status = "link_sent"
            else:
                logs.append("no phone on file; needs manual follow-up")
        elif step == "manual_review":
            logs.append("flagged for manual review")
    return {"status": status, "logs": logs}


def _build():
    g = StateGraph(State)
    g.add_node("analyze", analyze)
    g.add_node("plan", plan)
    g.add_node("execute", execute)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "plan")
    g.add_edge("plan", "execute")
    g.add_edge("execute", END)
    return g.compile()


agent = _build()


def run(failure: dict) -> State:
    """Run the agent and persist the outcome to memory."""
    try:
        out = agent.invoke({"failure": failure, "logs": []})
    except Exception as e:  # never lose a failure silently: record it for the dashboard
        memory.update(failure["id"], status="failed", logs=[f"agent error: {e!r}"])
        raise
    memory.update(failure["id"], category=out["classification"]["category"],
                  root_cause=out["classification"]["root_cause"], actions=out["plan"],
                  status=out["status"], logs=out["logs"])
    return out
