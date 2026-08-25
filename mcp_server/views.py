"""
HTTP surface for MCP: the connector endpoint plus student token management.

`McpEndpoint` is deliberately outside the DRF stack. It speaks JSON-RPC, not
REST: it has its own auth (a long-lived connector token, not a 60-minute JWT),
its own error envelope, and it must answer notifications with a bare 202.
Routing it through DRF's exception handler would rewrite those responses into
the REST `{code, detail}` shape and break clients.
"""

from __future__ import annotations

import json
import logging

from django.http import HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.generics import ListCreateAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from base.permissions import IsStudent
from mcp_server import protocol
from mcp_server.models import McpToken
from mcp_server.providers import (
    KNOWN_PROVIDER_IDS,
    connections_payload,
    disconnect_provider,
    issuer_for_request,
)
from mcp_server.serializers import (
    AssistantConnectionsSerializer,
    McpTokenCreateSerializer,
    McpTokenCreatedSerializer,
    McpTokenSerializer,
)

logger = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024


def _jsonrpc_error(code: int, message: str, http_status: int = 200) -> JsonResponse:
    return JsonResponse(
        {"jsonrpc": "2.0", "id": None, "error": {"code": code, "message": message}},
        status=http_status,
    )


@method_decorator(csrf_exempt, name="dispatch")
class McpEndpoint(View):
    """
    MCP Streamable HTTP endpoint.

    POST carries one JSON-RPC message (or, for older clients, a batch array).
    Requests get a JSON response; notifications get 202 with an empty body.
    """

    def get(self, request, *args, **kwargs):
        # We never initiate server-to-client streams, so there is no SSE channel
        # to open. The spec allows declining GET outright.
        response = HttpResponse(status=status.HTTP_405_METHOD_NOT_ALLOWED)
        response["Allow"] = "POST"
        return response

    def post(self, request, *args, **kwargs):
        token = self._bearer_token(request)
        if token is None:
            return self._unauthorized(request, "Missing bearer token.")

        mcp_token = McpToken.resolve(token)
        if mcp_token is None:
            return self._unauthorized(request, "Invalid, expired, or revoked token.")

        if len(request.body) > MAX_BODY_BYTES:
            return _jsonrpc_error(protocol.INVALID_REQUEST, "Request body too large.")

        try:
            payload = json.loads(request.body or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _jsonrpc_error(protocol.PARSE_ERROR, "Invalid JSON.")

        mcp_token.touch()
        student = mcp_token.student
        # Marks this request as MCP-authenticated for base.clients.resolve_channel.
        request.mcp_token = mcp_token

        if isinstance(payload, list):
            # Batching was dropped in the 2025-06-18 spec but older clients send it.
            if not payload:
                return _jsonrpc_error(protocol.INVALID_REQUEST, "Empty batch.")
            responses = [
                response
                for response in (
                    protocol.handle_message(message, student) for message in payload
                )
                if response is not None
            ]
            if not responses:
                return HttpResponse(status=status.HTTP_202_ACCEPTED)
            return JsonResponse(responses, safe=False)

        response = protocol.handle_message(payload, student)
        if response is None:
            # A notification. Acknowledge with no body.
            return HttpResponse(status=status.HTTP_202_ACCEPTED)
        return JsonResponse(response)

    @staticmethod
    def _bearer_token(request) -> str | None:
        header = request.headers.get("Authorization", "")
        prefix = "Bearer "
        if not header.startswith(prefix):
            return None
        token = header[len(prefix):].strip()
        return token or None

    @staticmethod
    def _unauthorized(request, message: str) -> JsonResponse:
        response = _jsonrpc_error(
            protocol.INVALID_REQUEST, message, http_status=status.HTTP_401_UNAUTHORIZED
        )
        # RFC 9728: point an unauthenticated client at the resource metadata so
        # it can discover the authorization server and start the OAuth flow
        # itself. Without this a hosted connector has no way to know where to go.
        metadata = (
            f"{issuer_for_request(request)}"
            "/.well-known/oauth-protected-resource"
        )
        response["WWW-Authenticate"] = (
            f'Bearer realm="lundrii-mcp", resource_metadata="{metadata}"'
        )
        return response


class McpTokenListCreateView(ListCreateAPIView):
    """List the student's connector tokens, or mint a new one."""

    permission_classes = [IsAuthenticated, IsStudent]
    serializer_class = McpTokenSerializer

    def get_queryset(self):
        return McpToken.objects.filter(
            student=self.request.user.student, revoked_at__isnull=True
        )

    @extend_schema(
        request=McpTokenCreateSerializer,
        responses={201: McpTokenCreatedSerializer},
        description=(
            "Mint a connector token. The plaintext token is returned once, in "
            "this response, and cannot be retrieved again."
        ),
    )
    def post(self, request, *args, **kwargs):
        serializer = McpTokenCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        token, plaintext = McpToken.issue(
            student=request.user.student,
            name=serializer.validated_data["name"],
            expires_at=serializer.validated_data.get("expires_at"),
        )
        payload = McpTokenSerializer(token).data
        payload["token"] = plaintext
        return Response(payload, status=status.HTTP_201_CREATED)


class McpTokenRevokeView(APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Token revoked.")},
    )
    def delete(self, request, token_id):
        token = (
            McpToken.objects.filter(pk=token_id, student=request.user.student)
            .filter(revoked_at__isnull=True)
            .first()
        )
        if token is None:
            return Response(
                {"code": "NOT_FOUND", "detail": "Token not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        token.revoke()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AssistantConnectionListView(APIView):
    """Connection status for ChatGPT / Claude, driven by live OAuth refresh grants."""

    permission_classes = [IsAuthenticated, IsStudent]
    serializer_class = AssistantConnectionsSerializer

    @extend_schema(
        responses=AssistantConnectionsSerializer,
        description=(
            "Setup URL and per-provider status. Connected means a live, unrevoked "
            "OAuth refresh token for a client classified as that product — not a "
            "one-hour access token."
        ),
    )
    def get(self, request):
        payload = connections_payload(
            request=request, student=request.user.student
        )
        return Response(AssistantConnectionsSerializer(payload).data)


class AssistantConnectionDisconnectView(APIView):
    permission_classes = [IsAuthenticated, IsStudent]

    @extend_schema(
        request=None,
        responses={204: OpenApiResponse(description="Provider disconnected.")},
        description=(
            "Revoke every OAuth refresh-token chain for this provider, and the "
            "matching access tokens. Personal paste tokens are not affected."
        ),
    )
    def delete(self, request, provider_id):
        if provider_id not in KNOWN_PROVIDER_IDS:
            return Response(
                {"code": "NOT_FOUND", "detail": "Unknown assistant provider."},
                status=status.HTTP_404_NOT_FOUND,
            )
        disconnect_provider(student=request.user.student, provider_id=provider_id)
        return Response(status=status.HTTP_204_NO_CONTENT)
