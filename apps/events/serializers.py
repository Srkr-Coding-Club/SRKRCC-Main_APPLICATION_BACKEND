from rest_framework import serializers, viewsets, permissions
from .models import Event

class EventSerializer(serializers.ModelSerializer):
    form_slug = serializers.CharField(source='registration_form.slug', read_only=True)
    form_title = serializers.CharField(source='registration_form.title', read_only=True)

    class Meta:
        model = Event
        fields = [
            'id', 'title', 'slug', 'description', 'category', 'venue',
            'capacity', 'poster_image', 'start_time', 'end_time',
            'visible_from', 'visible_until', 'registration_form',
            'form_slug', 'form_title', 'created_at', 'updated_at',
        ]

class EventViewSet(viewsets.ModelViewSet):
    queryset = Event.objects.select_related('registration_form').all()
    serializer_class = EventSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'slug'
