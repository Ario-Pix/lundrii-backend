from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response


class StandardPagination(PageNumberPagination):
    """
    System-wide pagination.

    Query params: page, page_size (max 100).
    Response: { count, next, previous, page, page_size, results }
    """

    page_size = 20
    page_size_query_param = "page_size"
    max_page_size = 100
    page_query_param = "page"

    def get_paginated_response(self, data):
        return Response(
            {
                "count": self.page.paginator.count,
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "page": self.page.number,
                "page_size": self.get_page_size(self.request),
                "results": data,
            }
        )

    def get_paginated_response_schema(self, schema):
        return {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "example": 100},
                "next": {
                    "type": "string",
                    "nullable": True,
                    "format": "uri",
                },
                "previous": {
                    "type": "string",
                    "nullable": True,
                    "format": "uri",
                },
                "page": {"type": "integer", "example": 1},
                "page_size": {"type": "integer", "example": 20},
                "results": schema,
            },
        }
