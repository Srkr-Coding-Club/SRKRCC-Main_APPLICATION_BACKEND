from django.db import models
from apps.core.models import TimeStampedModel
from apps.forms.models import Form

class Event(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    description = models.TextField()
    category = models.CharField(max_length=100, default='Workshop')
    venue = models.CharField(max_length=200, default='Campus Auditorium')
    capacity = models.PositiveIntegerField(default=100)
    poster_image = models.URLField(blank=True, null=True)
    
    start_time = models.DateTimeField()
    end_time = models.DateTimeField()
    visible_from = models.DateTimeField(blank=True, null=True)
    visible_until = models.DateTimeField(blank=True, null=True)
    
    registration_form = models.ForeignKey(Form, on_delete=models.SET_NULL, null=True, blank=True)

    def __str__(self):
        return self.title
