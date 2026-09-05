from rest_framework import viewsets
from .models import Event
from .serializers import EventSerializer
from apps.core.permissions import IsAdminOrClubLeadOrReadOnly

class EventViewSet(viewsets.ModelViewSet):
    queryset = Event.objects.all()
    serializer_class = EventSerializer
    permission_classes = [IsAdminOrClubLeadOrReadOnly]
    lookup_field = 'slug'
