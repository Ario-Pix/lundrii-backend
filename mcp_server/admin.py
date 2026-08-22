from django.contrib import admin

from mcp_server.models import McpToken


@admin.register(McpToken)
class McpTokenAdmin(admin.ModelAdmin):
    list_display = ("name", "student", "token_hint", "last_used_at", "expires_at", "revoked_at")
    list_filter = ("is_active", "revoked_at")
    search_fields = ("name", "student__name", "student__user__email")
    readonly_fields = ("token_hash", "token_hint", "created_at", "updated_at", "last_used_at")
