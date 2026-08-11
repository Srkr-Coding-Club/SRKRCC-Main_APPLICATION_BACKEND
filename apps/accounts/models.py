from django.contrib.auth.models import AbstractUser
from django.db import models
from apps.core.models import TimeStampedModel

class UserRole(models.TextChoices):
    MEMBER = 'MEMBER', 'Member'
    VOLUNTEER = 'VOLUNTEER', 'Volunteer'
    JUDGE = 'JUDGE', 'Judge'
    CLUB_LEAD = 'CLUB_LEAD', 'Club Lead'
    ADMIN = 'ADMIN', 'Admin'

class User(AbstractUser, TimeStampedModel):
    email = models.EmailField(unique=True)
    role = models.CharField(
        max_length=20,
        choices=UserRole.choices,
        default=UserRole.MEMBER
    )
    roll_number = models.CharField(max_length=50, blank=True, null=True)
    branch = models.CharField(max_length=100, blank=True, null=True)
    year = models.IntegerField(blank=True, null=True)
    phone_number = models.CharField(max_length=20, blank=True, null=True)
    github_profile = models.URLField(blank=True, null=True)
    linkedin_profile = models.URLField(blank=True, null=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"
