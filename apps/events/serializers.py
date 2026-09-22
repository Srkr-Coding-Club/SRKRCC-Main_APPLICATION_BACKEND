from django.utils import timezone
from rest_framework import serializers
from .models import Event

class EventSerializer(serializers.ModelSerializer):
    form_slug = serializers.CharField(source='registration_form.slug', read_only=True)
    form_title = serializers.CharField(source='registration_form.title', read_only=True)
    # Distinct from start_time/end_time (when the event itself happens):
    # these are when the linked Form accepts submissions at all — a workshop
    # on Sept 25 might stop taking registrations on Sept 20. Sourced straight
    # from the Form so the event card can label both without a second fetch.
    registration_opens_at = serializers.DateTimeField(source='registration_form.open_at', read_only=True)
    registration_closes_at = serializers.DateTimeField(source='registration_form.close_at', read_only=True)
    registration_count = serializers.IntegerField(read_only=True, default=0)
    is_hidden = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            'id', 'title', 'slug', 'description', 'category', 'venue',
            'capacity', 'poster_image', 'status', 'start_time', 'end_time',
            'visible_from', 'visible_until', 'is_hidden', 'registration_form',
            'form_slug', 'form_title', 'registration_opens_at', 'registration_closes_at',
            'registration_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status']

    def get_is_hidden(self, obj) -> bool:
        """True if this event is currently outside its visible_from/visible_until
        window — i.e. what EventViewSet.get_queryset() filters out for the
        public. Computed here so the admin UI doesn't have to reimplement the
        window logic; only meaningful to admins since a hidden event is never
        even returned to a non-admin caller in the first place."""
        now = timezone.now()
        if obj.visible_from and obj.visible_from > now:
            return True
        if obj.visible_until and obj.visible_until <= now:
            return True
        return False
