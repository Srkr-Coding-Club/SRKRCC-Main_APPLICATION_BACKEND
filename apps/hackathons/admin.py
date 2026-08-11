from django.contrib import admin
from .models import Hackathon, Team, Submission

@admin.register(Hackathon)
class HackathonAdmin(admin.ModelAdmin):
    list_display = ['title', 'is_flagship', 'theme', 'start_date', 'end_date']
    list_filter = ['is_flagship', 'start_date']

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ['name', 'hackathon', 'leader']

@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ['project_title', 'team', 'score']
