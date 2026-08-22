from django.http import JsonResponse


def health(_request):
    """Lightweight probe for Railway — no DB, no auth."""
    return JsonResponse({"status": "ok"})
