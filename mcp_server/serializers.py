"""Serializers for connector-token management."""

from django.utils import timezone
from rest_framework import serializers

from mcp_server.models import McpToken


class McpTokenSerializer(serializers.ModelSerializer):
    """A token as it can safely be read back — never the secret itself."""

    class Meta:
        model = McpToken
        fields = (
            "id",
            "name",
            "token_hint",
            "created_at",
            "last_used_at",
            "expires_at",
        )
        read_only_fields = fields


class McpTokenCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    expires_at = serializers.DateTimeField(required=False, allow_null=True)

    def validate_expires_at(self, value):
        if value is not None and value <= timezone.now():
            raise serializers.ValidationError("Expiry must be in the future.")
        return value


class McpTokenCreatedSerializer(McpTokenSerializer):
    """The creation response, which carries the plaintext token exactly once."""

    token = serializers.CharField(
        read_only=True,
        help_text="Paste this into the assistant. It is not retrievable later.",
    )

    class Meta(McpTokenSerializer.Meta):
        fields = McpTokenSerializer.Meta.fields + ("token",)
        read_only_fields = fields


class AssistantProviderSerializer(serializers.Serializer):
    id = serializers.ChoiceField(choices=["chatgpt", "claude", "unknown"])
    label = serializers.CharField()
    status = serializers.ChoiceField(choices=["connected", "disconnected"])
    openUrl = serializers.CharField(allow_blank=True)
    steps = serializers.ListField(child=serializers.CharField())
    connectedAt = serializers.DateTimeField(allow_null=True)


class AssistantConnectionsSerializer(serializers.Serializer):
    mcpUrl = serializers.URLField()
    providers = AssistantProviderSerializer(many=True)
