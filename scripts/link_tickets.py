"""Build the linked Nimbus ticket set from the Tobi-Bueck dataset (PLAN 4.2.1).

Pipeline (deterministic, no LLM — template rewriting only):
1. Load the CSV from data/raw/ (gitignored), keep English rows.
2. Bucket into Nimbus SaaS topics by keyword rules (first match wins).
3. Rewrite retail/platform wording to Nimbus wording (REWRITE_RULES).
4. Assign each ticket a seeded customer; for billing/refund/plan tickets
   inject the real invoice id + amount of an invoice that actually exists
   for that customer, so tools can answer from source rows.
5. Write data/tickets_all.jsonl (the load set, >= 80) and hold out every
   Nth ticket into data/dev/dev_tickets.jsonl (tuning split, disjoint by
   construction).

The dataset's `answer` column is NOT used (style reference only, see PLAN).

    uv run python -m scripts.link_tickets
"""

from __future__ import annotations

import csv
import hashlib
import json
import random
import re
from collections import Counter
from pathlib import Path
from typing import Any

from app.config import settings
from app.db import connect

CSV_PATH = Path("data/raw/customer-support-tickets-multilang.csv")
OUT_PATH = Path("data/tickets_all.jsonl")
DEV_PATH = Path("data/dev/dev_tickets.jsonl")

SELECTION_SEED = 7
DEV_EVERY_NTH = 4
TOPIC_CAPS: dict[str, int] = {
    "refund_out_of_window": 10,
    "refund_in_window": 10,
    "invoice_question": 12,
    "plan_change": 12,
    "login_account_access": 10,
    "sync_issues": 10,
    "service_outage": 12,
    "api_integration": 10,
    "security_incident": 8,
    "feature_request": 6,
    "paper_invoice_unsupported": 2,
}
OFF_DOMAIN_RULE = re.compile(
    r"\bhospital\b|\bmedical\b|\bpatient\b|\bhadoop\b|\bhealthcare\b|\bclinical\b",
    re.IGNORECASE,
)

# Specific rules first; BROADER_RULES then catch the rest.
SPECIFIC_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("paper_invoice_unsupported", re.compile(r"\bfax\b|paper copy|printed invoice", re.IGNORECASE)),
    ("refund_policy", re.compile(r"\brefund\b|money back|chargeback|charge back", re.IGNORECASE)),
    ("security_incident", re.compile(
        r"data breach|security incident|unauthori[sz]ed access|compromis(?:ed|e)|"
        r"malware|ransomware|vulnerability|cyberattack|hadoop|medical data|patient data",
        re.IGNORECASE,
    )),
]
BROADER_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("feature_request", re.compile(r"feature request|it would be nice|would love to see|\bsuggestion\b", re.IGNORECASE)),
    ("login_account_access", re.compile(r"password|2fa|two-factor|\bsso\b|locked out|cannot log ?in|can'?t (?:log ?in|access my account)", re.IGNORECASE)),
    ("api_integration", re.compile(r"\bapi\b|api key|webhook|rate limit|developer token", re.IGNORECASE)),
    ("plan_change", re.compile(r"upgrade|downgrade|change my plan|switch (?:my )?plan|cancel my (?:subscription|plan)|move me to", re.IGNORECASE)),
    ("invoice_question", re.compile(r"\binvoice\b|receipt|charged (?:twice|wrong)|double-?charg", re.IGNORECASE)),
    ("sync_issues", re.compile(r"synchroni[sz]|(?:data|file)s? (?:do sync|does not|not )?(?:appear|syncing)|\bsync\b", re.IGNORECASE)),
    ("service_outage", re.compile(r"outage|service (?:is )?down|not accessible|major disruption|unavailable", re.IGNORECASE)),
]

REWRITE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"Dear (?:Customer )?(?:Support|Customer Service|Customer Care) Team,\s*"), "Hi Nimbus Support,\n\n"),
    (re.compile(r"Customer Service Team,\s*"), "Hi Nimbus Support,\n\n"),
    (re.compile(r"<br\s*/?>|</?p>|</?b>", re.IGNORECASE), "\n"),
    (re.compile(r"<(?:tel_num|phone)>|\[(?:tel )?num(?:ber)?\]", re.IGNORECASE), "(555) 010-{num:04d}"),
    (re.compile(r"<acc_num>|\[account number\]", re.IGNORECASE), "{ref}"),
    (re.compile(r"<email>", re.IGNORECASE), "{email}"),
    (re.compile(r"<name>|\[name\]|\[customer name\]|\[your name\]", re.IGNORECASE), "{first}"),
    (re.compile(r"\[date\]", re.IGNORECASE), "{date}"),
    (re.compile(r"\[your company(?: name)?\]|\[company\]"), "Nimbus"),
    (re.compile(r"\[current platform\]|\[product name\]", re.IGNORECASE), "Nimbus"),
    (re.compile(r"\b(?:the|your) store\b", re.IGNORECASE), "your workspace"),
    (re.compile(r"\b(?:the|your) platform\b|\bSaaS platform\b|\bthe app\b", re.IGNORECASE), "the Nimbus workspace"),
    (re.compile(r"\bthe client\b", re.IGNORECASE), "your workspace"),
    (re.compile(r"\baccount management portal\b", re.IGNORECASE), "Nimbus workspace"),
    (re.compile(r"\bRAID controller\b", re.IGNORECASE), "Nimbus data connector"),
    (re.compile(r"\bdevice recognition\b", re.IGNORECASE), "integration recognition"),
    (re.compile(r"\bdevices?\b", re.IGNORECASE), "client applications"),
    (re.compile(r"\bhardware purchases\b", re.IGNORECASE), "workspace add-ons"),
    (re.compile(r"\boffice gadgets\b", re.IGNORECASE), "the desktop client"),
    (re.compile(r"\boffice devices\b", re.IGNORECASE), "client apps"),
]

AMOUNT_SENTENCE_TEMPLATES = [
    "I was charged ${amount:.2f} on {issued:%Y-%m-%d} for invoice {invoice_id} — please sort this out.",
    "Invoice {invoice_id} (charged {issued:%B %d}) still shows ${amount:.2f} on my card statement.",
    "Reference: invoice {invoice_id}, ${amount:.2f}, billed on {issued:%d %b %Y}.",
    "My billing history shows ${amount:.2f} on {issued:%Y-%m-%d} under invoice {invoice_id}.",
]
PLAN_SENTENCE_TEMPLATES = [
    "My current plan is {plan} ({cycle}) at ${amount:.2f} per month.",
    "Right now I am on the {plan} plan, {cycle}, paying ${amount:.2f} monthly.",
]
HEAVY_REFUND_HINTS = ("full refund", "money back", "charge back", "chargeback")


def pick_topic(subject: str, body: str) -> str:
    text = f"{subject}\n{body}"
    for name, rule in SPECIFIC_RULES:
        if rule.search(text):
            return name
    for name, rule in BROADER_RULES:
        if rule.search(text):
            return name
    return "feature_request"


def is_in_scope(subject: str, body: str) -> bool:
    """Keep source rows that can be honestly rewritten as Nimbus SaaS tickets."""
    return OFF_DOMAIN_RULE.search(f"{subject}\n{body}") is None


def rewrite_text(text: str, fmt: dict[str, Any]) -> str:
    out = text
    for rule, replacement in REWRITE_RULES:
        out = rule.sub(lambda m, replacement=replacement: replacement.format_map(fmt), out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def billable_sentence(kind: str, cand: dict[str, Any], rng: random.Random) -> str:
    template = rng.choice(AMOUNT_SENTENCE_TEMPLATES if kind == "amount" else PLAN_SENTENCE_TEMPLATES)
    values = dict(cand)
    values["amount"] = cand.get("amount_cents", 0) / 100
    values["invoice_id"] = cand.get("invoice_id", "not available")
    values["issued"] = cand.get("issued_at")
    return " " + template.format(**values)


def fetch_invoice_candidates(conn: Any) -> dict[str, list[dict[str, Any]]]:
    """Deterministic invoice facts (from source rows) used for injection."""
    w = settings.refund_window_days
    out: dict[str, list[dict[str, Any]]] = {}
    out["fresh"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.external_ref, c.first_name, c.last_name, c.email,
                   i.id AS invoice_id, i.amount_cents, i.issued_at
            FROM customers c
            JOIN invoices i ON i.customer_id = c.id
            WHERE c.status = 'active' AND i.status = 'paid'
              AND p.status = 'succeeded'
              AND p.paid_at > %s - make_interval(days => %s)
              AND NOT EXISTS (SELECT 1 FROM payments p
                              WHERE p.invoice_id = i.id AND p.amount_refunded_cents > 0)
            ORDER BY c.external_ref, i.id
            """,
            (settings.as_of, w),
        ).fetchall()
    ]
    out["stale"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.external_ref, c.first_name, c.last_name, c.email,
                   i.id AS invoice_id, i.amount_cents,                    i.issued_at, p.paid_at
            FROM customers c
            JOIN invoices i ON i.customer_id = c.id
            JOIN payments p ON p.invoice_id = i.id
            WHERE c.status = 'active' AND i.status = 'paid'
              AND p.status = 'succeeded'
              AND p.paid_at <= %s - make_interval(days => %s)
            ORDER BY c.external_ref, i.id
            """,
            (settings.as_of, w),
        ).fetchall()
    ]
    out["generic"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT c.external_ref, c.first_name, c.last_name, c.email,
                   i.id AS invoice_id, i.amount_cents, i.issued_at,
                   p.code AS plan, s.billing_cycle AS cycle
            FROM customers c
            JOIN invoices i ON i.customer_id = c.id
            JOIN subscriptions s ON s.id = i.subscription_id
            JOIN plans p ON p.id = s.plan_id
            WHERE c.status = 'active' AND i.status = 'paid'
            ORDER BY c.external_ref, i.id
            """
        ).fetchall()
    ]
    out["annual"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT DISTINCT ON (c.external_ref)
                   c.external_ref, c.first_name, c.last_name, c.email,
                   p.code AS plan, s.billing_cycle AS cycle,
                   p.monthly_price_cents AS amount_cents
            FROM customers c
            JOIN subscriptions s ON s.customer_id = c.id AND s.status = 'active'
            JOIN plans p ON p.id = s.plan_id
            WHERE c.status = 'active' AND s.billing_cycle = 'annual'
            ORDER BY c.external_ref, s.created_at
            """
        ).fetchall()
    ]
    out["any_active"] = [
        dict(r)
        for r in conn.execute(
            """
            SELECT external_ref, first_name, last_name, email FROM customers
            WHERE status = 'active' ORDER BY external_ref
            """
        ).fetchall()
    ]
    return out


def link_tickets() -> dict[str, Any]:
    if not CSV_PATH.exists():
        raise RuntimeError(f"dataset missing: {CSV_PATH} — fetch the Tobi-Bueck CSV first")
    rng = random.Random(SELECTION_SEED)
    with connect(settings.database_url) as conn:
        invoices = fetch_invoice_candidates(conn)

    with CSV_PATH.open() as f:
        rows = list(csv.DictReader(f))
    en_rows = [
        (i, r)
        for i, r in enumerate(rows)
        if r["language"] == "en" and is_in_scope(r["subject"], r["body"])
    ]
    print(f"dataset rows: {len(rows)} | english/in-scope: {len(en_rows)}")

    buckets: dict[str, list[tuple[int, dict[str, str]]]] = {}
    for idx, r in en_rows:
        buckets.setdefault(pick_topic(r["subject"], r["body"]), []).append((idx, r))
    for topic_rows in buckets.values():
        rng.shuffle(topic_rows)

    # Refund asks split deterministically: alternating fresh / out-of-window,
    # so both pools exercise the window rule with real invoice facts.
    refund_items = buckets.pop("refund_policy", [])
    heavy = [t for t in refund_items if any(k in f"{t[1]['subject']} {t[1]['body']}".lower() for k in HEAVY_REFUND_HINTS)]
    light = [t for t in refund_items if t not in heavy]
    buckets["refund_in_window"] = [t for j, t in enumerate(heavy) if j % 2 == 0]
    buckets["refund_out_of_window"] = [t for j, t in enumerate(heavy) if j % 2 == 1]
    # Non-heavy refund mentions are billing questions, not action asks.
    buckets["invoice_question"] = buckets.get("invoice_question", []) + light

    linked: list[dict[str, Any]] = []
    counters: Counter[str] = Counter()
    pool_idx = {"fresh": 0, "stale": 0, "generic": 0, "annual": 0, "any_active": 0}
    seen_hashes: set[str] = set()

    def rotate(pool: str) -> dict[str, Any] | None:
        cands = invoices[pool]
        i = pool_idx[pool]
        pool_idx[pool] += 1
        return cands[i % len(cands)] if cands else None

    for topic, cap in TOPIC_CAPS.items():
        for idx, r in buckets.get(topic, [])[:cap]:
            subject, body = r["subject"], r["body"]
            ref: str | None = None
            extra = ""
            if topic == "refund_in_window" and invoices["fresh"]:
                cand = rotate("fresh")
            elif topic == "refund_out_of_window" and invoices["stale"]:
                cand = rotate("stale")
            elif topic == "invoice_question" and invoices["generic"]:
                cand = rotate("generic")
            elif topic == "plan_change" and invoices["annual"]:
                cand = rotate("annual")
            else:
                cand = rotate("any_active")
            if cand is not None:
                ref = cand["external_ref"]
                fmt = {
                    "first": cand.get("first_name") or "there",
                    "email": cand.get("email") or "",
                    "ref": ref,
                    "num": (int(ref[2:]) if ref and ref[2:].isdigit() else 1234),
                    "date": "this billing cycle",
                }
                body = rewrite_text(body, fmt)
                subject = rewrite_text(subject, fmt)
                if topic in ("refund_in_window", "refund_out_of_window", "invoice_question"):
                    extra = billable_sentence("amount", cand, rng)
                elif topic == "plan_change":
                    extra = billable_sentence("plan", cand, rng)
            else:
                fmt = {"first": "there", "email": "unknown@example.nimbus.test", "ref": "C-0000",
                       "num": 1234, "date": "this billing cycle"}
                body = rewrite_text(body, fmt)
                subject = rewrite_text(subject, fmt)
            body = (body + extra).strip()
            digest = hashlib.sha256(body.encode()).hexdigest()
            if digest in seen_hashes or len(body) < 40:
                continue
            seen_hashes.add(digest)
            linked.append({
                "external_ref": ref,
                "from_email": cand.get("email") if cand is not None else None,
                "from_name": (
                    f"{cand.get('first_name', '')} {cand.get('last_name', '')}".strip()
                    if cand is not None else None
                ),
                "channel": "email",
                "subject": subject.strip() or topic.replace("_", " ").title(),
                "body": body,
                "topic": topic,
                # This is normalized by code, not manually reviewed by a person.
                "reviewed": False,
                "priority_hint": r["priority"],
                "type_hint": r["type"],
                "queue_hint": r["queue"],
                "tags": [r[f"tag_{i}"] for i in range(1, 4) if r[f"tag_{i}"]],
                "source_dataset": "Tobi-Bueck/customer-support-tickets",
                "source_row": idx,
                "version": "template-rewrite-1",
            })
            counters[topic] += 1

    # Deterministic received order (most recent first) for the loader.
    for i, t in enumerate(linked):
        t["received_hours_ago"] = 2 + (i * 5) % 400
    linked.sort(key=lambda t: t["received_hours_ago"])

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUT_PATH.open("w") as f_all, DEV_PATH.open("w") as f_dev:
        dev_count = 0
        for i, t in enumerate(linked):
            if i % DEV_EVERY_NTH == 3:
                f_dev.write(json.dumps(t) + "\n")
                dev_count += 1
            else:
                f_all.write(json.dumps(t) + "\n")

    load_count = len(linked) - dev_count
    print(f"linked tickets: {load_count} (load) + {dev_count} (dev split) = {len(linked)}")
    print("by topic:", dict(counters))
    return {"total": len(linked), "dev": dev_count, "topics": dict(counters)}


if __name__ == "__main__":
    link_tickets()
