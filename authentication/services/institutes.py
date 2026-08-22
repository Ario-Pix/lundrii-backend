"""Resolve an institute from a student email domain."""

from __future__ import annotations

from laundry.models import Institute


def normalize_email_domain(value: str) -> str:
    return (value or "").strip().lower().lstrip("@")


def email_domain(email: str) -> str:
    email = (email or "").strip().lower()
    if "@" not in email:
        return ""
    return normalize_email_domain(email.rsplit("@", 1)[-1])


def domains_of(institute: Institute) -> list[str]:
    out: list[str] = []
    for item in institute.allowed_email_domains or []:
        if not isinstance(item, str):
            continue
        domain = normalize_email_domain(item)
        if domain:
            out.append(domain)
    return out


def collect_allowed_domains() -> list[str]:
    """Union of active institutes' allow-lists, stable order."""
    seen: set[str] = set()
    ordered: list[str] = []
    for inst in Institute.objects.filter(is_active=True).iterator():
        for domain in domains_of(inst):
            if domain not in seen:
                seen.add(domain)
                ordered.append(domain)
    return ordered


def resolve_institute_for_email(email: str) -> Institute | None:
    domain = email_domain(email)
    if not domain:
        return None
    for inst in Institute.objects.filter(is_active=True).iterator():
        if domain in domains_of(inst):
            return inst
    return None
