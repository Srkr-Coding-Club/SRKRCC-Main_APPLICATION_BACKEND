from django.contrib import admin
from .models import (
    Hackathon, HackathonAnnouncement, ProblemStatement, Round, RoundEntry,
    Submission, Team, TeamInvite, TeamMember,
)


@admin.register(Hackathon)
class HackathonAdmin(admin.ModelAdmin):
    list_display = ['title', 'is_flagship', 'theme', 'status', 'start_date', 'end_date']
    list_filter = ['is_flagship', 'status', 'start_date']


class TeamMemberInline(admin.TabularInline):
    model = TeamMember
    extra = 0
    fields = ['user', 'role', 'joined_at']
    readonly_fields = ['joined_at']
    raw_id_fields = ['user']


@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    list_display = ['name', 'hackathon', 'leader', 'status', 'problem_statement', 'is_open_innovation']
    list_filter = ['hackathon', 'status']
    search_fields = ['name', 'leader__email']
    inlines = [TeamMemberInline]


@admin.register(ProblemStatement)
class ProblemStatementAdmin(admin.ModelAdmin):
    list_display = ['code', 'title', 'domain', 'hackathon', 'max_teams', 'is_active']
    readonly_fields = ['code']
    list_filter = ['hackathon', 'is_active']


@admin.register(TeamInvite)
class TeamInviteAdmin(admin.ModelAdmin):
    list_display = ['team', 'invited_user', 'status', 'created_at']
    list_filter = ['status', 'hackathon']


@admin.register(Round)
class RoundAdmin(admin.ModelAdmin):
    list_display = ['hackathon', 'order', 'name', 'status', 'results_published']
    list_filter = ['hackathon', 'status']


@admin.register(RoundEntry)
class RoundEntryAdmin(admin.ModelAdmin):
    list_display = ['round', 'team', 'status', 'decided_at']
    list_filter = ['round__hackathon', 'status']


@admin.register(HackathonAnnouncement)
class HackathonAnnouncementAdmin(admin.ModelAdmin):
    list_display = ['title', 'hackathon', 'audience', 'is_active', 'publish_at']
    list_filter = ['hackathon', 'audience', 'is_active']


@admin.register(Submission)
class SubmissionAdmin(admin.ModelAdmin):
    list_display = ['project_title', 'team', 'score']
