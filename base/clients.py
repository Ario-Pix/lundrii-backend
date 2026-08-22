"""
Work out which client a request came from, without asking the API to say so.

The goal is that `Booking.channel` fills itself in. Callers never pass a channel
in a request body, because a body field is both easy to forget and easy to spoof
into whatever a client feels like reporting.

Resolution, most trustworthy first:

1. **MCP.** The request authenticated with an `McpToken` at `/mcp/`. This is a
   fact about which credential was presented, so it cannot be misreported.
2. **The `client` claim in the JWT.** Stamped at login and signed, so it is
   fixed for the life of the token and survives refresh. This is the normal path
   for the mobile and web apps.
3. **The `X-Client-Platform` header.** An escape hatch for a client holding a
   token issued before it started declaring itself.
4. **User-Agent sniffing.** A guess, for clients that declare nothing.
5. **`app`.** The existing default.

Honesty about what this is: telemetry, not a security boundary. Steps 2–4 all
originate with the caller, so a determined client can claim to be an iPhone. It
answers "roughly where do bookings come from", which is what the admin analytics
asks. Nothing is authorised on the basis of it.
"""

from __future__ import annotations

import re

CLIENT_HEADER = "X-Client-Platform"
JWT_CLIENT_CLAIM = "client"

# Channel values, duplicated as plain strings so this module stays importable
# from authentication/ without dragging in laundry's models.
CHANNEL_APP = "app"
CHANNEL_ANDROID = "android"
CHANNEL_IOS = "ios"
CHANNEL_WEBSITE = "website"
CHANNEL_MCP = "mcp"

VALID_CHANNELS = frozenset(
    {CHANNEL_APP, CHANNEL_ANDROID, CHANNEL_IOS, CHANNEL_WEBSITE, CHANNEL_MCP}
)

# What a client may call itself -> what we store.
_PLATFORM_ALIASES = {
    "android": CHANNEL_ANDROID,
    "ios": CHANNEL_IOS,
    "iphone": CHANNEL_IOS,
    "ipad": CHANNEL_IOS,
    "web": CHANNEL_WEBSITE,
    "website": CHANNEL_WEBSITE,
    "browser": CHANNEL_WEBSITE,
    "mcp": CHANNEL_MCP,
    "app": CHANNEL_APP,
}

# Ordered: the first match wins, so specific native-app markers are checked
# before the generic browser ones. Android's WebView UA also contains "Mozilla",
# which is why "android" is tested first.
_USER_AGENT_PATTERNS = (
    (re.compile(r"\blundrii[-/ ]?android\b", re.I), CHANNEL_ANDROID),
    (re.compile(r"\blundrii[-/ ]?ios\b", re.I), CHANNEL_IOS),
    (re.compile(r"\bandroid\b", re.I), CHANNEL_ANDROID),
    (re.compile(r"\b(iphone|ipad|ipod|cfnetwork|darwin)\b", re.I), CHANNEL_IOS),
    (re.compile(r"\b(mozilla|chrome|safari|firefox|edge)\b", re.I), CHANNEL_WEBSITE),
)


def normalise_platform(raw: str | None) -> str | None:
    """Map a client-supplied platform name onto a channel, or None."""
    if not raw:
        return None
    return _PLATFORM_ALIASES.get(str(raw).strip().lower())


def channel_from_user_agent(user_agent: str | None) -> str | None:
    if not user_agent:
        return None
    for pattern, channel in _USER_AGENT_PATTERNS:
        if pattern.search(user_agent):
            return channel
    return None


def declared_channel(request) -> str | None:
    """
    What this request says it is, ignoring any token.

    Used at login to decide what to stamp into the JWT.
    """
    return normalise_platform(
        request.headers.get(CLIENT_HEADER)
    ) or channel_from_user_agent(request.headers.get("User-Agent"))


def resolve_channel(request) -> str:
    """The channel to record for a booking made by this request."""
    # 1. An MCP-authenticated request is MCP, whatever it claims.
    if getattr(request, "mcp_token", None) is not None:
        return CHANNEL_MCP

    # 2. The signed claim from login.
    token = getattr(request, "auth", None)
    if token is not None:
        try:
            claimed = normalise_platform(token.get(JWT_CLIENT_CLAIM))
        except (AttributeError, TypeError):
            claimed = None
        if claimed:
            return claimed

    # 3 & 4. Anything the request itself offers.
    return declared_channel(request) or CHANNEL_APP
