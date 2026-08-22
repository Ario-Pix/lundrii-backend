"""Student-facing connector-token routes (mounted under /api/v1/)."""

from django.urls import path

from mcp_server import views

urlpatterns = [
    path(
        "me/mcp-tokens",
        views.McpTokenListCreateView.as_view(),
        name="student-mcp-tokens",
    ),
    path(
        "me/mcp-tokens/<uuid:token_id>",
        views.McpTokenRevokeView.as_view(),
        name="student-mcp-token-revoke",
    ),
    path(
        "me/assistant-connections",
        views.AssistantConnectionListView.as_view(),
        name="student-assistant-connections",
    ),
    path(
        "me/assistant-connections/<str:provider_id>",
        views.AssistantConnectionDisconnectView.as_view(),
        name="student-assistant-connection-disconnect",
    ),
]
