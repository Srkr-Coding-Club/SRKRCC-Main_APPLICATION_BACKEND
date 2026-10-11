from rest_framework import viewsets
from .models import BlogPost
from .serializers import BlogPostSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly
from apps.core.cache import PublicReadCacheControlMixin

class BlogPostViewSet(PublicReadCacheControlMixin, viewsets.ModelViewSet):
    queryset = BlogPost.objects.all()
    serializer_class = BlogPostSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'
