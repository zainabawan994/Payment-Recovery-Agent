"""Failure classifier: Gemini turns Stripe error data + memory into a category and root cause."""
import json
from functools import cache
from typing import Literal

from google import genai
from pydantic import BaseModel

Category = Literal[
    "insufficient_funds", "expired_card", "incorrect_details", "authentication_required",
    "processing_error", "fraud_suspected", "generic_decline",
]


class Classification(BaseModel):
    category: Category
    root_cause: str


SYSTEM = (
    "You classify failed card payments for a payment recovery agent. You get Stripe's error data "
    "for one failure plus up to five past failures (the same customer's first, then similar ones). "
    "Pick the single category that best explains this failure and state the likely root cause in "
    "one sentence. Stripe's decline_code is the strongest signal; use the history to spot patterns, "
    "such as repeated insufficient funds or many different cards failing for one customer."
)


@cache
def _client():
    # reads GEMINI_API_KEY; 30s timeout so a stuck call can't hang the webhook worker forever
    return genai.Client(http_options={"timeout": 30_000, "retry_options": {"attempts": 2}})


# Newest first; fall back when a model is overloaded (503/504) or out of free-tier quota (429).
MODELS = ("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.1-flash-lite")


def classify(failure: dict, history: list[dict]) -> Classification:
    for model in MODELS:
        try:
            resp = _client().models.generate_content(
                model=model,
                contents=json.dumps({"failure": failure, "past_failures": history}, default=str),
                config={"system_instruction": SYSTEM, "response_mime_type": "application/json",
                        "response_schema": Classification},
            )
            break
        except genai.errors.APIError as e:
            if model == MODELS[-1] or not (e.code == 429 or e.code >= 500):
                raise
    if not isinstance(resp.parsed, Classification):  # blocked or unparseable answer
        return Classification(category="generic_decline", root_cause="Classifier gave no answer; defaulted to generic decline.")
    return resp.parsed
