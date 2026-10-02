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
    path('teams/<int:pk>/<str:team_action>/', views.TeamActionView.as_view(), name='hackathon-team-action'),

    path('<slug:slug>/my-team/', views.MyTeamView.as_view(), name='hackathon-my-team'),
    path('<slug:slug>/teams/', views.HackathonTeamsView.as_view(), name='hackathon-teams'),
    path('<slug:slug>/user-lookup/', views.UserLookupView.as_view(), name='hackathon-user-lookup'),
    path('<slug:slug>/stats/', views.HackathonStatsView.as_view(), name='hackathon-stats'),

    path('<slug:slug>/problem-statements/', views.ProblemStatementListView.as_view(), name='hackathon-ps-list'),
    path('<slug:slug>/problem-statements/<int:pk>/', views.ProblemStatementDetailView.as_view(), name='hackathon-ps-detail'),

    path('<slug:slug>/rounds/', views.RoundListView.as_view(), name='hackathon-round-list'),
    path('<slug:slug>/rounds/<int:pk>/', views.RoundDetailView.as_view(), name='hackathon-round-detail'),
    path('<slug:slug>/rounds/<int:pk>/entries/', views.RoundEntriesView.as_view(), name='hackathon-round-entries'),
    path('<slug:slug>/rounds/<int:pk>/<str:round_action>/', views.RoundActionView.as_view(), name='hackathon-round-action'),

    path('<slug:slug>/announcements/', views.AnnouncementListView.as_view(), name='hackathon-announcement-list'),
    path('<slug:slug>/announcements/<int:pk>/', views.AnnouncementDetailView.as_view(), name='hackathon-announcement-detail'),
    path('<slug:slug>/announcements/<int:pk>/notify/', views.AnnouncementNotifyView.as_view(), name='hackathon-announcement-notify'),
] + router.urls
