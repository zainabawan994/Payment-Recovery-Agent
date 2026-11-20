"""Agent tools: card check, retry, checkout link, WhatsApp notify."""
import os
import time

import httpx
import stripe

stripe.api_key = os.getenv("STRIPE_SECRET_KEY")


def bin_check(card: dict, risk_level: str | None) -> dict:
    # Uses the card metadata Stripe already returned (brand/funding/country/expiry) plus Radar's risk level,
    # so no card data leaves Stripe for a third-party BIN service.
    now = time.gmtime()
    expired = bool(card.get("exp_year")) and (card["exp_year"], card.get("exp_month", 12)) < (now.tm_year, now.tm_mon)
    return {
        "valid": not expired,
        "risky": risk_level in ("elevated", "highest"),
        "brand": card.get("brand"), "funding": card.get("funding"), "country": card.get("country"),
    }


def retry_payment(payment_intent_id: str, payment_method_id: str) -> bool:
    try:
        pi = stripe.PaymentIntent.confirm(payment_intent_id, payment_method=payment_method_id, off_session=True,
                                          idempotency_key=f"recovery-retry-{payment_intent_id}")
    except stripe.CardError:
        return False
    return pi.status == "succeeded"


def create_checkout_link(amount: int, currency: str, customer_id: str | None, failure_id: str) -> str:
    session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        line_items=[{"price_data": {"currency": currency, "unit_amount": amount,
                                    "product_data": {"name": "Outstanding payment"}}, "quantity": 1}],
        payment_intent_data={"metadata": {"failure_id": failure_id}},  # lets the webhook mark it recovered
        success_url=os.environ["CHECKOUT_SUCCESS_URL"],
        idempotency_key=f"recovery-link-{failure_id}",
    )
    return session.url


def send_whatsapp(phone: str, amount_text: str, link: str) -> str:
    # Business-initiated WhatsApp messages must use a Meta-approved template.
    r = httpx.post(
        f"https://graph.facebook.com/v21.0/{os.environ['WHATSAPP_PHONE_ID']}/messages",
        headers={"Authorization": f"Bearer {os.environ['WHATSAPP_TOKEN']}"},
        json={
            "messaging_product": "whatsapp",
            "to": phone.lstrip("+"),
            "type": "template",
            "template": {
                "name": os.getenv("WHATSAPP_TEMPLATE", "payment_recovery"),
                "language": {"code": os.getenv("WHATSAPP_LANG", "en")},
                "components": [{"type": "body", "parameters": [
                    {"type": "text", "text": amount_text}, {"type": "text", "text": link}]}],
            },
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["messages"][0]["id"]
