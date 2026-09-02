from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import RegisterView, ProfileView, UserListView, CustomTokenObtainPairView
from .views_import import (
    MemberImportPreviewView,
    MemberImportCommitView,
    MemberImportJobDetailView,
    ClubIdNextPreviewView,
    ClubIdValidateView,
    ReferralStatsView,
    EmailTemplateListCreateView,
    EmailDispatchView,
    EmailJobDetailView,
)

urlpatterns = [
    # Auth & Profile
    path('register/', RegisterView.as_view(), name='auth_register'),
    path('login/', CustomTokenObtainPairView.as_view(), name='token_obtain_pair'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', ProfileView.as_view(), name='auth_me'),
    path('users/', UserListView.as_view(), name='auth_users_list'),

    # Member Directory Backup Import Pipeline
    path('members/import/preview/', MemberImportPreviewView.as_view(), name='member_import_preview'),
    path('members/import/commit/', MemberImportCommitView.as_view(), name='member_import_commit'),
    path('members/import/jobs/<uuid:job_id>/', MemberImportJobDetailView.as_view(), name='member_import_job_detail'),

    # Club ID Endpoints (SRKR Coding Club permanent identity)
    path('club-ids/next/', ClubIdNextPreviewView.as_view(), name='club_id_next'),
    path('club-ids/validate/', ClubIdValidateView.as_view(), name='club_id_validate'),

    # Referral Tracking
    path('referrals/stats/', ReferralStatsView.as_view(), name='referrals_stats'),

    # Universal Email Automation Endpoints
    path('email-templates/', EmailTemplateListCreateView.as_view(), name='email_templates_list_create'),
    path('emails/send/', EmailDispatchView.as_view(), name='emails_dispatch'),
    path('emails/jobs/<uuid:job_id>/', EmailJobDetailView.as_view(), name='email_job_detail'),
]
