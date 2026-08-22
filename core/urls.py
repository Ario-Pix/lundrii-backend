"""URL configuration for Lundrii core."""

from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from base.views import health
from mcp_server import oauth_views
from mcp_server.views import McpEndpoint

urlpatterns = [
    path("health/", health, name="health"),
    # admin panel
    path("admin/", admin.site.urls),
    # Student / admin REST. MCP OAuth discovery and /mcp/ stay at the domain
    # root — hosted connectors fetch those by well-known path, not from /api/v1.
    path("api/v1/auth/", include("authentication.urls")),
    path("api/v1/", include("mcp_server.urls")),
    path("api/v1/", include("laundry.urls")),
    # OAuth discovery. Both must sit at the domain root — a connector fetches
    # them by well-known path, not from anything we tell it.
    path(
        ".well-known/oauth-protected-resource",
        oauth_views.ProtectedResourceMetadata.as_view(),
        name="oauth-protected-resource-metadata",
    ),
    path(
        ".well-known/oauth-authorization-server",
        oauth_views.AuthorizationServerMetadata.as_view(),
        name="oauth-authorization-server-metadata",
    ),
    path("oauth/register", oauth_views.RegisterClient.as_view(), name="oauth-register"),
    path("oauth/authorize", oauth_views.Authorize.as_view(), name="oauth-authorize"),
    path("oauth/token", oauth_views.Token.as_view(), name="oauth-token"),
    # MCP connector endpoint. Deliberately outside /api/v1: it speaks JSON-RPC,
    # authenticates with a connector token rather than a JWT, and is versioned
    # by the MCP protocol handshake rather than by URL.
    path("mcp/", McpEndpoint.as_view(), name="mcp-endpoint"),
    # documentation
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
