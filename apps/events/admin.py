from django.contrib import admin
from .models import Event

@admin.register(Event)
class EventAdmin(admin.ModelAdmin):
    list_display = ['title', 'category', 'venue', 'start_time', 'end_time']
    list_filter = ['category', 'start_time']
    search_fields = ['title', 'venue']
