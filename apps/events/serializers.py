from django.db import transaction
from django.utils import timezone
from rest_framework import serializers
from .models import Event
from .quick_registration import (
    QUICK_FIELDS,
    apply_quick_registration,
    is_quick_form,
    quick_field_keys,
    sync_quick_form_details,
)

class EventSerializer(serializers.ModelSerializer):
    form_slug = serializers.CharField(source='registration_form.slug', read_only=True)
    form_title = serializers.CharField(source='registration_form.title', read_only=True)
    # Distinct from start_time/end_time (when the event itself happens):
    # these are when the linked Form accepts submissions at all - a workshop
    # on Sept 25 might stop taking registrations on Sept 20. Sourced straight
    # from the Form so the event card can label both without a second fetch.
    registration_opens_at = serializers.DateTimeField(source='registration_form.open_at', read_only=True)
    registration_closes_at = serializers.DateTimeField(source='registration_form.close_at', read_only=True)
    registration_count = serializers.IntegerField(read_only=True, default=0)
    is_hidden = serializers.SerializerMethodField()
    # One-click registration. Write: the member details to collect (name and email are always
    # included). The server turns that into the event's registration form. Read: whether the event is
    # one-click and which details it collects, so the admin form and the public page can show them.
    quick_registration_fields = serializers.ListField(
        child=serializers.ChoiceField(choices=list(QUICK_FIELDS)),
        required=False, write_only=True, allow_empty=True,
    )
    is_quick_registration = serializers.SerializerMethodField()
    registration_profile_fields = serializers.SerializerMethodField()

    class Meta:
        model = Event
        fields = [
            'id', 'title', 'slug', 'description', 'category', 'venue',
            'capacity', 'poster_image', 'status', 'start_time', 'end_time',
            'visible_from', 'visible_until', 'is_hidden', 'registration_form',
            'form_slug', 'form_title', 'registration_opens_at', 'registration_closes_at',
            'registration_count', 'quick_registration_fields', 'is_quick_registration',
            'registration_profile_fields', 'created_at', 'updated_at',
        ]
        read_only_fields = ['status']

    def get_is_quick_registration(self, obj) -> bool:
        return is_quick_form(obj.registration_form)

    def get_registration_profile_fields(self, obj) -> list[str]:
        return quick_field_keys(obj.registration_form)

    def validate(self, attrs):
        if attrs.get('quick_registration_fields') and attrs.get('registration_form'):
            raise serializers.ValidationError({
                'quick_registration_fields': 'Choose either a registration form or one-click registration, not both.',
            })
        return attrs

    def _link_quick_registration(self, event, quick_fields):
        """Create or update the one-click form when asked to; otherwise just keep an existing one in step."""
        if quick_fields:
            request = self.context.get('request')
            form = apply_quick_registration(event, quick_fields, getattr(request, 'user', None))
            event.registration_form = form  # also replaces any stale cached form instance
            event.save(update_fields=['registration_form', 'updated_at'])
        else:
            sync_quick_form_details(event)

    def create(self, validated_data):
        quick_fields = validated_data.pop('quick_registration_fields', None)
        with transaction.atomic():
            event = super().create(validated_data)
            self._link_quick_registration(event, quick_fields)
        return event

    def update(self, instance, validated_data):
        quick_fields = validated_data.pop('quick_registration_fields', None)
        with transaction.atomic():
            event = super().update(instance, validated_data)
            self._link_quick_registration(event, quick_fields)
        return event

    def get_is_hidden(self, obj) -> bool:
        """True if this event is currently outside its visible_from/visible_until
        window - i.e. what EventViewSet.get_queryset() filters out for the
        public. Computed here so the admin UI doesn't have to reimplement the
        window logic; only meaningful to admins since a hidden event is never
        even returned to a non-admin caller in the first place."""
        now = timezone.now()
        if obj.visible_from and obj.visible_from > now:
            return True
        if obj.visible_until and obj.visible_until <= now:
            return True
        return False
