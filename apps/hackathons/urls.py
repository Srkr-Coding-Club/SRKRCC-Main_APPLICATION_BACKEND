from django.urls import path
from rest_framework.routers import DefaultRouter

from . import views

router = DefaultRouter()
router.register(r'submissions', views.SubmissionViewSet, basename='hackathon-submission')
router.register(r'', views.HackathonViewSet, basename='hackathon')

# Explicit paths come before the router so fixed prefixes (my-invites/,
# teams/<id>/, invites/<id>/) are matched before the `{slug}` detail route.
urlpatterns = [
    path('my-invites/', views.MyInvitesView.as_view(), name='hackathon-my-invites'),
    path('my-teams/', views.MyTeamsView.as_view(), name='hackathon-my-teams'),
    path('invites/<int:pk>/<str:decision>/', views.InviteRespondView.as_view(), name='hackathon-invite-respond'),

    path('teams/<int:pk>/', views.TeamDetailView.as_view(), name='hackathon-team-detail'),
    path('teams/<int:pk>/invite-by-email/', views.TeamEmailInviteView.as_view(), name='hackathon-team-email-invite'),
    path('teams/<int:pk>/<str:team_action>/', views.TeamActionView.as_view(), name='hackathon-team-action'),

    path('<slug:slug>/stages/', views.HackathonStagesView.as_view(), name='hackathon-stages'),
    path('<slug:slug>/timeline/', views.StageListView.as_view(), name='hackathon-timeline'),
    path('<slug:slug>/timeline/reorder/', views.StageReorderView.as_view(), name='hackathon-timeline-reorder'),
    path('<slug:slug>/timeline/import-rounds/', views.StageImportRoundsView.as_view(), name='hackathon-timeline-import'),
    path('<slug:slug>/timeline/<int:pk>/', views.StageDetailView.as_view(), name='hackathon-timeline-detail'),
    path('<slug:slug>/my-team/', views.MyTeamView.as_view(), name='hackathon-my-team'),
    path('<slug:slug>/teams/', views.HackathonTeamsView.as_view(), name='hackathon-teams'),
    path('<slug:slug>/user-lookup/', views.UserLookupView.as_view(), name='hackathon-user-lookup'),
    path('<slug:slug>/stats/', views.HackathonStatsView.as_view(), name='hackathon-stats'),
    path('<slug:slug>/team-emails/', views.TeamEmailSettingsView.as_view(), name='hackathon-team-emails'),
    path('<slug:slug>/team-emails/test/', views.TeamEmailTestView.as_view(), name='hackathon-team-emails-test'),
    path('<slug:slug>/remind-forming/', views.FormingTeamsReminderView.as_view(), name='hackathon-remind-forming'),

    path('<slug:slug>/problem-statements/', views.ProblemStatementListView.as_view(), name='hackathon-ps-list'),
    path('<slug:slug>/problem-statements/upload/', views.ProblemStatementUploadView.as_view(), name='hackathon-ps-upload'),
    path('<slug:slug>/problem-statements/reorder/', views.ProblemStatementReorderView.as_view(), name='hackathon-ps-reorder'),
    path('<slug:slug>/problem-statements/<int:pk>/', views.ProblemStatementDetailView.as_view(), name='hackathon-ps-detail'),

    path('<slug:slug>/rounds/', views.RoundListView.as_view(), name='hackathon-round-list'),
    path('<slug:slug>/rounds/<int:pk>/', views.RoundDetailView.as_view(), name='hackathon-round-detail'),
    path('<slug:slug>/rounds/<int:pk>/entries/', views.RoundEntriesView.as_view(), name='hackathon-round-entries'),
    path('<slug:slug>/rounds/<int:pk>/<str:round_action>/', views.RoundActionView.as_view(), name='hackathon-round-action'),

    path('<slug:slug>/announcements/', views.AnnouncementListView.as_view(), name='hackathon-announcement-list'),
    path('<slug:slug>/announcements/<int:pk>/', views.AnnouncementDetailView.as_view(), name='hackathon-announcement-detail'),
    path('<slug:slug>/announcements/<int:pk>/notify/', views.AnnouncementNotifyView.as_view(), name='hackathon-announcement-notify'),
] + router.urls
