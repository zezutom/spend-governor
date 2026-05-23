"""Tools for Helpdesk Co-Pilot.

All tools return synthetic data — no external calls. ADK wraps plain
Python callables passed to LlmAgent(tools=[...]), using the docstring as
the LLM-visible tool description and the type-annotated signature as the
parameter schema.

Tools are deliberately honest about their behavior: refund tickets should
resolve through kb_lookup of /policies/refunds + customer_lookup, not
through repeated web_search. The anti-pattern that breaks this gets baked
in on Day 3.
"""

import uuid


# -- Task classification ----------------------------------------------------
#
# Classification chosen as a *tool* the agent must call first, rather than
# free-text emission from the model. Reasons:
#   - Tool calls produce structured span data with both args and return
#     value in trace attributes. Day 5 aggregation reads task_class
#     directly from the task_classifier span; no text parsing.
#   - Keyword logic here is deterministic and demo-stable. The four task
#     types are well-separated by obvious keywords, so a thin classifier
#     is enough — we are not trying to be a real intent model.

def task_classifier(message: str) -> dict:
    """Classify a customer support ticket. MUST be called first, before
    any other tool, on every new ticket.

    Returns a dict with key "task_class", one of:
      - "password_reset" — customer can't log in or wants to reset a password
      - "refund_handling" — customer wants money back or to reverse a charge
      - "plan_change" — customer wants to upgrade, downgrade, or switch plans
      - "account_question" — anything else (account settings, product questions, billing questions that are not refunds)

    Use the returned task_class to choose your downstream workflow.
    """
    lower = message.lower()
    if any(k in lower for k in ("password", "reset my", "can't log in", "cant log in", "can't sign in", "locked out")):
        return {"task_class": "password_reset"}
    if any(k in lower for k in ("refund", "money back", "reverse the charge", "charge back", "charged me")):
        return {"task_class": "refund_handling"}
    if any(k in lower for k in ("upgrade", "downgrade", "change my plan", "switch plan", "switch to pro", "switch to enterprise", "cancel my subscription")):
        return {"task_class": "plan_change"}
    return {"task_class": "account_question"}


# -- Knowledge base ---------------------------------------------------------

_KB_ARTICLES = {
    "/policies/refunds": (
        "Stratus Forms refund policy. Refunds are available within 30 days "
        "of the charge for paid plans (Pro, Enterprise). Free plan accounts "
        "are not eligible. Refunds for Enterprise contracts beyond 30 days "
        "require manager approval and should be escalated. Partial refunds "
        "(prorated for unused time) are available on plan downgrades during "
        "an active billing cycle."
    ),
    "/account/password-reset": (
        "To reset a Stratus Forms password: navigate to the login page, "
        "click 'Forgot password?', enter the account email address, and "
        "follow the link in the reset email. Reset links expire after one "
        "hour. If the user does not receive the email, check spam first; "
        "if still missing, the support agent can trigger a manual reset."
    ),
    "/billing/plan-changes": (
        "Plan changes take effect immediately. Upgrades are billed the "
        "prorated difference for the remainder of the current cycle. "
        "Downgrades issue a prorated credit toward the next invoice; no "
        "cash refund is issued automatically on downgrade unless the "
        "customer requests one and meets refund policy criteria."
    ),
    "/account/general": (
        "Stratus Forms account settings, billing address, team member "
        "management, and SSO configuration are all available under "
        "Settings → Account. Account owners can transfer ownership; "
        "billing contacts can update payment methods."
    ),
}


def kb_lookup(path: str) -> dict:
    """Look up an article in the Stratus Forms internal knowledge base.

    Use this tool for anything covered by Stratus Forms' own documentation:
    refund policy, account procedures, billing rules, product features,
    plan-change mechanics. The KB is pre-indexed and fast.

    Args:
        path: KB article path, e.g. "/policies/refunds",
              "/account/password-reset", "/billing/plan-changes",
              "/account/general".

    Returns a dict with key "article" containing the article text, or
    {"status": "not_found"} if no article matches the path.
    """
    if path in _KB_ARTICLES:
        return {"article": _KB_ARTICLES[path]}
    return {"status": "not_found"}


# -- Web search -------------------------------------------------------------

def web_search(query: str) -> list[dict]:
    """Search the open web via a third-party search API.

    Use this for general external information not covered by Stratus Forms'
    own documentation — e.g. industry definitions, third-party integration
    details, public technical references. Do NOT use this for Stratus Forms
    policies, procedures, or product behavior; use kb_lookup for those.

    Args:
        query: Free-text search query.

    Returns a list of 3-4 results, each a dict with keys "title", "url",
    and "snippet".
    """
    return [
        {
            "title": f"{query} — Overview | Wikipedia",
            "url": f"https://en.wikipedia.org/wiki/{query.replace(' ', '_')}",
            "snippet": (
                f"This article provides a general overview of {query} and "
                "related concepts, including history, common usage, and "
                "links to further reading."
            ),
        },
        {
            "title": f"How to handle {query} — Stack Exchange",
            "url": "https://softwareengineering.stackexchange.com/questions/12345",
            "snippet": (
                f"A community-voted answer on best practices around "
                f"{query}, with examples drawn from common SaaS workflows."
            ),
        },
        {
            "title": f"{query} explained — Medium",
            "url": "https://medium.com/@author/explained-abc123",
            "snippet": (
                f"A long-form blog post discussing {query}, common pitfalls, "
                "and recommended approaches for small teams."
            ),
        },
        {
            "title": f"{query} — Reddit discussion",
            "url": "https://reddit.com/r/SaaS/comments/abcdef",
            "snippet": (
                f"A thread of practitioner anecdotes about {query}, "
                "including a few dissenting opinions."
            ),
        },
    ]


# -- Customer lookup --------------------------------------------------------

_CUSTOMERS = {
    "ACME-001": {
        "name": "ACME Corp",
        "plan": "pro",
        "mrr": 49.00,
        "signup_date": "2025-08-12",
        "recent_ticket_count": 3,
    },
    "GLOBEX-001": {
        "name": "Globex",
        "plan": "enterprise",
        "mrr": 499.00,
        "signup_date": "2024-03-04",
        "recent_ticket_count": 1,
    },
    "INITECH-001": {
        "name": "Initech",
        "plan": "free",
        "mrr": 0.00,
        "signup_date": "2026-01-15",
        "recent_ticket_count": 7,
    },
    "STARK-001": {
        "name": "Stark Industries",
        "plan": "enterprise",
        "mrr": 999.00,
        "signup_date": "2023-11-20",
        "recent_ticket_count": 0,
    },
    "WONKA-001": {
        "name": "Wonka Industries",
        "plan": "pro",
        "mrr": 49.00,
        "signup_date": "2025-09-30",
        "recent_ticket_count": 2,
    },
    "WAYNE-001": {
        "name": "Wayne Enterprises",
        "plan": "enterprise",
        "mrr": 1499.00,
        "signup_date": "2022-06-11",
        "recent_ticket_count": 4,
    },
}


def customer_lookup(customer_id: str) -> dict:
    """Fetch a customer record from the Stratus Forms CRM.

    Use this when you need account context — plan tier, MRR, signup date,
    recent ticket activity — to inform a decision (e.g. refund eligibility,
    escalation tier).

    Args:
        customer_id: Customer ID, e.g. "ACME-001", "GLOBEX-001".

    Returns the customer record dict (name, plan, mrr, signup_date,
    recent_ticket_count), or {"status": "not_found"} if the ID is unknown.
    """
    if customer_id in _CUSTOMERS:
        return dict(_CUSTOMERS[customer_id])
    return {"status": "not_found"}


# -- Refund API -------------------------------------------------------------

def refund_api(customer_id: str, amount_usd: float, reason: str) -> dict:
    """Issue a refund through the Stratus Forms billing system.

    Only call this after confirming refund eligibility against the refund
    policy (kb_lookup of /policies/refunds) and the customer record
    (customer_lookup). Always succeeds in this demo environment.

    Args:
        customer_id: Customer ID to refund.
        amount_usd: Refund amount in USD.
        reason: Short reason string for the refund, recorded in billing.

    Returns {"status": "ok", "refund_id": <uuid>, "amount_usd": <amount>}.
    """
    return {
        "status": "ok",
        "refund_id": str(uuid.uuid4()),
        "amount_usd": amount_usd,
    }


# -- Ticket update ----------------------------------------------------------

def ticket_update(
    ticket_id: str,
    status: str,
    internal_note: str = "",
    customer_reply: str = "",
) -> dict:
    """Update a ticket's status and optionally send a reply to the customer.

    Call this to close out a ticket once it has been resolved. The
    customer_reply is the public message sent to the customer; the
    internal_note is for the support audit log.

    Args:
        ticket_id: Ticket identifier.
        status: New status — typically "resolved", "pending_customer",
                or "in_progress".
        internal_note: Optional note saved to the internal audit log.
        customer_reply: Optional message sent to the customer.

    Returns {"status": "ok", "ticket_id": <id>}.
    """
    return {"status": "ok", "ticket_id": ticket_id}


# -- Escalation -------------------------------------------------------------

def escalate_human(ticket_id: str, reason: str) -> dict:
    """Route a ticket to a human support agent.

    Use this when the ticket is outside automated handling: edge cases
    not covered by policy, manager-approval-required refunds, technical
    bugs, or customer requests that need human judgment.

    Args:
        ticket_id: Ticket identifier.
        reason: Short reason string explaining why escalation is needed.

    Returns {"status": "escalated", "ticket_id": <id>,
             "queue": "human-tier-2"}.
    """
    return {
        "status": "escalated",
        "ticket_id": ticket_id,
        "queue": "human-tier-2",
    }
