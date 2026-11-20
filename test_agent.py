"""Offline self-check of the state machine: external services are stubbed."""
import pytest

import graph
import tools
from classifier import Classification


@pytest.fixture
def stubs(monkeypatch):
    calls = {"whatsapp": [], "retry": [], "updates": []}
    monkeypatch.setattr(graph.memory, "recall", lambda *a: calls.get("history", []))
    monkeypatch.setattr(graph.memory, "update", lambda fid, **kw: calls["updates"].append(kw))
    monkeypatch.setattr(tools, "create_checkout_link", lambda *a: "https://checkout.test/x")
    monkeypatch.setattr(tools, "send_whatsapp", lambda *a: calls["whatsapp"].append(a) or "wamid.1")
    monkeypatch.setattr(tools, "retry_payment", lambda *a: calls["retry"].append(a) or calls.get("retry_ok", False))

    def set_category(cat):
        monkeypatch.setattr(graph, "classify", lambda f, h: Classification(category=cat, root_cause="test"))
    calls["set_category"] = set_category
    return calls


def failure(**kw):
    base = {"id": "f1", "payment_intent_id": "pi_1", "payment_method_id": "pm_1", "customer_id": "cus_1",
            "phone": "+923001234567", "amount": 2500, "currency": "usd", "decline_code": "x", "message": "m",
            "risk_level": "normal", "card": {"exp_year": 2099, "exp_month": 1}}
    return base | kw


def test_transient_error_retries_and_recovers(stubs):
    stubs["set_category"]("processing_error")
    stubs["retry_ok"] = True
    out = graph.run(failure())
    assert out["status"] == "recovered" and stubs["retry"] and not stubs["whatsapp"]
    assert stubs["updates"][-1]["status"] == "recovered"


def test_failed_retry_falls_back_to_whatsapp_link(stubs):
    stubs["set_category"]("processing_error")
    out = graph.run(failure())
    assert out["status"] == "link_sent"
    assert stubs["whatsapp"][0] == ("+923001234567", "25.00 USD", "https://checkout.test/x")


def test_insufficient_funds_never_retries(stubs):
    stubs["set_category"]("insufficient_funds")
    assert graph.run(failure())["status"] == "link_sent" and not stubs["retry"]


def test_expired_card_skips_retry_even_for_transient(stubs):
    stubs["set_category"]("processing_error")
    graph.run(failure(card={"exp_year": 2001, "exp_month": 1}))
    assert not stubs["retry"]


def test_fraud_and_risky_and_repeat_customers_go_to_review(stubs):
    stubs["set_category"]("fraud_suspected")
    assert graph.run(failure())["status"] == "manual_review"
    stubs["set_category"]("insufficient_funds")
    assert graph.run(failure(risk_level="highest"))["status"] == "manual_review"
    stubs["history"] = [{"customer_id": "cus_1"}] * 3
    assert graph.run(failure())["status"] == "manual_review"
    assert not stubs["whatsapp"]


def test_no_phone_means_manual_follow_up(stubs):
    stubs["set_category"]("insufficient_funds")
    out = graph.run(failure(phone=None))
    assert out["status"] == "manual_review" and not stubs["whatsapp"]


def test_agent_error_is_recorded(stubs, monkeypatch):
    stubs["set_category"]("insufficient_funds")
    monkeypatch.setattr(tools, "send_whatsapp", lambda *a: (_ for _ in ()).throw(RuntimeError("meta down")))
    with pytest.raises(RuntimeError):
        graph.run(failure())
    assert stubs["updates"][-1]["status"] == "failed"
