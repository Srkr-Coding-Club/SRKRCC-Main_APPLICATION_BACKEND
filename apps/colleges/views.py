from django.db.models import Case, IntegerField, Value, When
from rest_framework import generics, permissions
from apps.core.throttling import ScopedRateThrottle

from apps.core.pagination import StandardPagination

from .models import DEFAULT_COLLEGE_CODE, College
from .serializers import CollegeSerializer
from .text import normalize_search_text

MAX_QUERY_LENGTH = 100


class CollegeListView(generics.ListAPIView):
    """
    GET /api/colleges/?q=&page=&page_size=

    Public (the signup page needs it before any account exists). Every word in
    `q` must appear in the name; names that start with `q` rank first. With no
    `q` the default college is pinned to the top of the first page.
    """
    serializer_class = CollegeSerializer
    # Public catalogue: skipping authentication stops an expired token cookie on
    # the signup page from turning an anonymous read into a 401.
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    pagination_class = StandardPagination
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'college_search'

    def get_queryset(self):
        query = normalize_search_text(self.request.query_params.get('q', '')[:MAX_QUERY_LENGTH])
        queryset = College.objects.all()

        if not query:
            pin_default = Case(
                When(code=DEFAULT_COLLEGE_CODE, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            )
            return queryset.annotate(rank=pin_default).order_by('rank', 'name', 'id')

        for word in query.split():
            queryset = queryset.filter(search_name__contains=word)
        prefix_first = Case(
            When(search_name__startswith=query, then=Value(0)),
            default=Value(1),
            output_field=IntegerField(),
        )
        return queryset.annotate(rank=prefix_first).order_by('rank', 'name', 'id')


class CollegeDetailView(generics.RetrieveAPIView):
    queryset = College.objects.all()
    serializer_class = CollegeSerializer
    # Public catalogue: skipping authentication stops an expired token cookie on
    # the signup page from turning an anonymous read into a 401.
    authentication_classes = []
    permission_classes = [permissions.AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'college_search'
