"""FastAPI entrypoint: Stripe webhook in, dashboard out."""
import json
import os
import secrets
from pathlib import Path

import stripe
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

import graph
import memory
import tools  # noqa: F401  (sets stripe.api_key)

app = FastAPI(title="Payment Recovery Agent")
basic = HTTPBasic()


def admin(creds: HTTPBasicCredentials = Depends(basic)):
    if not secrets.compare_digest(creds.password.encode(), os.environ["DASHBOARD_PASSWORD"].encode()):
        raise HTTPException(401, headers={"WWW-Authenticate": "Basic"})


@app.post("/webhook/stripe")
async def stripe_webhook(request: Request, bg: BackgroundTasks):
    payload = await request.body()
    try:
        stripe.Webhook.construct_event(payload, request.headers.get("stripe-signature", ""),
                                       os.environ["STRIPE_WEBHOOK_SECRET"])
    except (ValueError, stripe.SignatureVerificationError):
        raise HTTPException(400, "invalid signature")
    event = json.loads(payload)  # verified; plain dicts are simpler than StripeObject
    pi = event["data"]["object"]
    if event["type"] == "payment_intent.payment_failed":
        bg.add_task(handle_failure, event["id"], pi)  # ack fast; Stripe retries slow webhooks
    elif event["type"] == "payment_intent.succeeded":
        memory.mark_recovered(pi["id"], (pi.get("metadata") or {}).get("failure_id"))
    return {"received": True}


def handle_failure(event_id: str, pi: dict) -> None:
    # Failures of our own retries / recovery checkouts must not start a second recovery loop.
    if (pi.get("metadata") or {}).get("failure_id") or memory.open_failure_for(pi["id"]):
        return
    err = pi.get("last_payment_error") or {}
    pm = err.get("payment_method") or {}
    card = pm.get("card") or {}
    decline = err.get("decline_code") or err.get("code")
    customer_id = pi.get("customer")

    failure_id = memory.record_failure({
        "event_id": event_id, "payment_intent_id": pi["id"], "customer_id": customer_id,
        "amount": pi["amount"], "currency": pi["currency"], "decline_code": decline,
        "failure_message": err.get("message"),
        "embedding": memory.embed(f"{decline} {err.get('message')} {card.get('brand')} {card.get('funding')} {card.get('country')}"),
    })
    if failure_id is None:
        return  # duplicate delivery

    risk = stripe.Charge.retrieve(pi["latest_charge"]).outcome.risk_level if pi.get("latest_charge") else None
    phone = (stripe.Customer.retrieve(customer_id).phone if customer_id else None) \
        or (pm.get("billing_details") or {}).get("phone")
    graph.run({
        "id": failure_id, "payment_intent_id": pi["id"], "payment_method_id": pm.get("id"),
        "customer_id": customer_id, "phone": phone, "amount": pi["amount"], "currency": pi["currency"],
        "decline_code": decline, "message": err.get("message"), "risk_level": risk,
        "card": {k: card.get(k) for k in ("brand", "funding", "country", "exp_month", "exp_year")},
    })


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(admin)])
def dashboard():
    return (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")


@app.get("/api/metrics", dependencies=[Depends(admin)])
def metrics():
    return memory.metrics()
