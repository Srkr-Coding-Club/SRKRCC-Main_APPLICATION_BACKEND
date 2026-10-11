"""
Shared pagination for list endpoints that grow without bound (audit log, CodeQuest
submissions). Returns DRF's standard {count, next, previous, results} envelope.
"""
from rest_framework.pagination import PageNumberPagination


class StandardPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200
