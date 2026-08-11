from django.db import models
from django.conf import settings
from apps.core.models import TimeStampedModel

class BlogPost(TimeStampedModel):
    title = models.CharField(max_length=200)
    slug = models.SlugField(max_length=200, unique=True)
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='blog_posts')
    content = models.TextField()
    excerpt = models.TextField(blank=True)
    cover_image = models.URLField(blank=True, null=True)
    tags = models.JSONField(default=list, blank=True)
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(blank=True, null=True)

    def __str__(self):
        return self.title
