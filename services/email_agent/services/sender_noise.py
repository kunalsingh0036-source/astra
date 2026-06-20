"""Sender noise filter for the email-agent service.

A faithful port of astra/email/signals.py `_is_noise` — kept embedded
in this service (not imported across the repo) for the same reason as
voice.py: the email-agent deploys as its own Railway container and a
cross-package import is a path that resolves on the laptop but not in
the container.

Why triage needs this: the classifier marks plenty of automated mail
(bank alerts, build-failure notifications, "support@" autoresponders)
as action_needed. Drafting earnest replies to a noreply bot is exactly
the kind of output that makes the whole feature read as dumb. Triage
filters candidates through here so Astra only ever drafts replies to
addresses a human might actually answer.

Keep in sync with astra/email/signals.py and the mirror in
astra-web/app/api/email/unanswered/route.ts.
"""

from __future__ import annotations

import re

_NOISE_LOCAL = (
    "noreply", "no-reply", "no_reply", "donotreply", "do-not-reply",
    "donotrespond", "automated",
    "notification", "notifications",
    "alert", "alerts",
    "update", "updates",
    "news", "newsletter",
    "receipt", "receipts", "invoice", "billing",
    "marketing", "promo", "promotions", "offer", "offers",
    "campaign", "campaigns", "newcomers",
    "transaction", "transactions",
    "statements", "statement",
    "mailer", "mailers", "mail", "email",
    "info", "hello", "team", "support",
    "care", "customercare",
    "welcome", "onboarding",
    "account", "accounts",
    "emandates", "emandate",
    "instructor", "instructors",
    "sales",
)

_NOISE_DOMAIN = (
    "sbicard.com", "kotak.bank", "icicibank", "icici.bank",
    "hdfcbank", "hdfc.bank", "axisbank", "axis.bank",
    "yesbank", "yes.bank", "sbi.co", "sbi.bank", "bankalerts",
    "paytm.com", "phonepe.com", "gpay",
    "gst.gov.in", "incometax.gov.in",
    "facebookmail.com", "facebook.com", "meta.com",
    "linkedin.com", "linkedinmail",
    "slack.com", "medium.com", "substack.com",
    "github.com", "gitlab.com",
    "googleplay", "googlecommunity", "google.com/play",
    "youtube.com", "youtu.be",
    "appleid.apple.com", "apple.com",
    "anthropic.com", "openai.com",
    "cloudflare.com", "notion.so", "figma.com",
    "railway.app", "vercel.com", "render.com",
    "booking.com", "airbnb.com",
    "spicejet.com", "web-spicejet", "indigo.in", "airindia",
    "mmt.mp.makemytrip", "makemytrip.com",
    "uber.com", "olacabs",
    "amazon.in", "amazon.com", "flipkart.com", "myntra.com",
    "reliancedigital", "relianceretail",
    "tata1mg", "emaila.1mg.com", "1mg.com",
    "swiggy.in", "zomato.com",
    "freeletics", "updates.freeletics", "xpandstore",
    "email.intch", "intch.org",
    "myhq.in",
    "email.", ".campaign.", "send.", "sendgrid", "mailgun", "mailchimp",
    "razorpay.com",
)

_NOISE_SUBDOMAIN_PREFIX = (
    "updates.", "news.", "newsletter.", "email.", "mail.",
    "notifications.", "notify.", "alerts.", "info.", "marketing.",
    "campaigns.", "promo.", "offers.", "invoice.", "billing.",
    "account.", "accounts.", "noreply.", "support.",
)


def is_noise(from_address: str) -> bool:
    """True if `from_address` is an automated / noreply / blast sender
    that Astra should NOT draft a reply to."""
    s = (from_address or "").lower()
    if not s:
        return True

    m = re.search(r"<([^>]+)>", s)
    email_part = (m.group(1) if m else s).strip()
    if "@" in email_part:
        local, _, domain = email_part.partition("@")
    else:
        local, domain = email_part, ""

    for p in _NOISE_DOMAIN:
        if p in domain:
            return True
    for p in _NOISE_SUBDOMAIN_PREFIX:
        if domain.startswith(p):
            return True
    for p in _NOISE_LOCAL:
        if p in local:
            return True
    return False
