"""Supabase (pgvector) memory: past failures, recovery state, dashboard metrics."""
import os
from datetime import datetime, timedelta, timezone
from functools import cache

import voyageai
from supabase import create_client

TABLE = "payment_failures"


@cache
def db():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


@cache
def _voyage():
    return voyageai.Client()  # reads VOYAGE_API_KEY


def embed(text: str) -> list[float]:
    return _voyage().embed([text], model="voyage-3.5", input_type="document").embeddings[0]


def record_failure(row: dict) -> str | None:
    """Insert a new failure; returns its id, or None if this Stripe event was already seen."""
    res = db().table(TABLE).upsert(row, on_conflict="event_id", ignore_duplicates=True).execute()
    return res.data[0]["id"] if res.data else None


def open_failure_for(payment_intent_id: str) -> bool:
    res = (db().table(TABLE).select("id").eq("payment_intent_id", payment_intent_id)
           .neq("status", "recovered").limit(1).execute())
    return bool(res.data)


def recall(customer_id: str | None, text: str, exclude_id: str) -> list[dict]:
    return db().rpc("match_failures", {"query_embedding": embed(text), "customer": customer_id or "",
                                       "exclude": exclude_id, "k": 5}).execute().data


def update(failure_id: str, **fields) -> None:
    db().table(TABLE).update(fields).eq("id", failure_id).execute()


def mark_recovered(payment_intent_id: str, failure_id: str | None) -> None:
    fields = {"status": "recovered", "recovered_at": datetime.now(timezone.utc).isoformat()}
    if failure_id:  # paid through our checkout link
        db().table(TABLE).update(fields).eq("id", failure_id).execute()
    db().table(TABLE).update(fields).eq("payment_intent_id", payment_intent_id).execute()


def metrics() -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = (db().table(TABLE)
            .select("id,created_at,customer_id,amount,currency,decline_code,category,root_cause,status,actions,logs")
            .gte("created_at", since).order("created_at", desc=True).limit(500).execute().data)
    recovered = [r for r in rows if r["status"] == "recovered"]
    return {
        "total": len(rows),
        "recovered": len(recovered),
        "recovery_rate": round(100 * len(recovered) / len(rows), 1) if rows else 0,
        "recovered_amount": sum(r["amount"] or 0 for r in recovered),  # minor units, mixed currencies summed
        "by_status": {s: sum(r["status"] == s for r in rows) for s in {r["status"] for r in rows}},
        "rows": rows[:100],
    }
