from rest_framework import viewsets
from .models import JobListing
from .serializers import JobListingSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly

class JobListingViewSet(viewsets.ModelViewSet):
    queryset = JobListing.objects.all()
    serializer_class = JobListingSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'
