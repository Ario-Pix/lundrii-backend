"""OpenAPI post-processing for Lundrii."""

_METHODS = frozenset(
    {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
)


def tag_by_path(result, generator, request, public):
    """Group operations under Auth / Student / Admin from the URL path."""
    for path, path_item in result.get("paths", {}).items():
        if "/auth/" in path:
            tag = "Auth"
        elif "/admin/" in path:
            tag = "Admin"
        else:
            tag = "Student"
        for method, operation in path_item.items():
            if method not in _METHODS or not isinstance(operation, dict):
                continue
            operation["tags"] = [tag]
    return result
