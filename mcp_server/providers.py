"""
Classify MCP OAuth clients as ChatGPT / Claude, and the setup copy Profile shows.

Hosted connectors register themselves; we never see a student-chosen token name.
The only stable signal is the redirect URI host the product registered. Unmatched
clients stay `unknown` — guessing would mark ChatGPT connected because Claude
(or a random client) happened to look similar.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlparse

from django.conf import settings
from django.utils import timezone

from mcp_server.models import McpToken, OAuthClient, OAuthRefreshToken

CHATGPT = "chatgpt"
CLAUDE = "claude"
UNKNOWN = "unknown"

KNOWN_PROVIDER_IDS = (CHATGPT, CLAUDE)

CHATGPT_HOSTS = ("chatgpt.com", "openai.com")
CLAUDE_HOSTS = ("claude.ai", "anthropic.com")

CHATGPT_NAME_HINTS = ("chatgpt", "openai")
CLAUDE_NAME_HINTS = ("claude", "anthropic")

PROVIDER_SETUP = {
    CHATGPT: {
        "id": CHATGPT,
        "label": "ChatGPT",
        "openUrl": "https://chatgpt.com/",
        "steps": [
            "Open ChatGPT",
            "Settings → Connectors → add server",
            "Paste the MCP URL",
            "Approve Lundrii",
        ],
    },
    CLAUDE: {
        "id": CLAUDE,
        "label": "Claude",
        "openUrl": "https://claude.ai/settings/connectors",
        "steps": [
            "Open Claude",
            "Settings → Connectors → add a custom connector",
            "Paste the MCP URL",
            "Approve Lundrii",
        ],
    },
}


def issuer_for_request(request) -> str:
    """Same origin `_issuer` uses, with MCP_PUBLIC_URL when the proxy Host is wrong."""
    public = getattr(settings, "MCP_PUBLIC_URL", "") or ""
    public = public.strip().rstrip("/")
    if public:
        return public
    return f"{request.scheme}://{request.get_host()}"


def mcp_url_for_request(request) -> str:
    return f"{issuer_for_request(request)}/mcp/"


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    host = (host or "").lower().rstrip(".")
    if not host:
        return False
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _hosts_for_client(client: OAuthClient) -> list[str]:
    hosts: list[str] = []
    for uri in client.redirect_uris or []:
        hosts.append(urlparse(uri).hostname or "")
    if client.client_uri:
        hosts.append(urlparse(client.client_uri).hostname or "")
    return hosts


def classify_oauth_client(client: OAuthClient | None) -> str:
    """Return chatgpt, claude, or unknown. Conflicting signals stay unknown."""
    if client is None:
        return UNKNOWN

    hosts = _hosts_for_client(client)
    chatgpt = any(_host_matches(h, CHATGPT_HOSTS) for h in hosts)
    claude = any(_host_matches(h, CLAUDE_HOSTS) for h in hosts)

    name = (client.client_name or "").lower()
    if not chatgpt and not claude:
        if any(hint in name for hint in CHATGPT_NAME_HINTS):
            chatgpt = True
        if any(hint in name for hint in CLAUDE_NAME_HINTS):
            claude = True

    if chatgpt and not claude:
        return CHATGPT
    if claude and not chatgpt:
        return CLAUDE
    return UNKNOWN


def live_refresh_tokens(*, student):
    return OAuthRefreshToken.objects.filter(
        student=student,
        revoked_at__isnull=True,
        expires_at__gt=timezone.now(),
    ).select_related("client")


def connected_at_by_provider(*, student) -> dict[str, datetime]:
    """Earliest live refresh-token created_at per classified provider."""
    earliest: dict[str, datetime] = {}
    for row in live_refresh_tokens(student=student):
        provider_id = classify_oauth_client(row.client)
        existing = earliest.get(provider_id)
        if existing is None or row.created_at < existing:
            earliest[provider_id] = row.created_at
    return earliest


def connections_payload(*, request, student) -> dict:
    connected_at = connected_at_by_provider(student=student)
    providers = []
    for provider_id in KNOWN_PROVIDER_IDS:
        setup = PROVIDER_SETUP[provider_id]
        at = connected_at.get(provider_id)
        providers.append(
            {
                **setup,
                "status": "connected" if at is not None else "disconnected",
                "connectedAt": at,
            }
        )
    unknown_at = connected_at.get(UNKNOWN)
    if unknown_at is not None:
        providers.append(
            {
                "id": UNKNOWN,
                "label": "Other assistant",
                "openUrl": "",
                "steps": [],
                "status": "connected",
                "connectedAt": unknown_at,
            }
        )
    return {
        "mcpUrl": mcp_url_for_request(request),
        "providers": providers,
    }


def disconnect_provider(*, student, provider_id: str) -> None:
    """
    Cut the OAuth grant for every client classified as this provider.

    Refresh tokens are the durable credential (30 days). Access McpTokens expire
    in an hour, but a leftover one would still answer /mcp/ until then, so both
    are revoked. Personal paste tokens (oauth_client is null) are left alone.
    """
    client_ids: set = set()
    refresh_rows = list(
        OAuthRefreshToken.objects.filter(student=student).select_related("client")
    )
    for row in refresh_rows:
        if classify_oauth_client(row.client) == provider_id:
            client_ids.add(row.client_id)

    access_rows = McpToken.objects.filter(
        student=student, oauth_client__isnull=False
    ).select_related("oauth_client")
    for token in access_rows:
        if classify_oauth_client(token.oauth_client) == provider_id:
            client_ids.add(token.oauth_client_id)

    if not client_ids:
        return

    now = timezone.now()
    for row in refresh_rows:
        if row.client_id in client_ids and row.revoked_at is None:
            row.revoke_chain()

    McpToken.objects.filter(
        student=student,
        oauth_client_id__in=client_ids,
        revoked_at__isnull=True,
    ).update(revoked_at=now)
