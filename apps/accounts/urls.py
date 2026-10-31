from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView
from .views import RegisterView, ProfileView, UserListView, UserDetailView, CustomTokenObtainPairView, LogoutView
from .views_password_setup import (
    PasswordSetupRequestView,
    PasswordSetupVerifyView,
    PasswordSetupConfirmView,
)
from .views_club_ids import (
    AllocateView,
    AssignView,
    ClearView,
    ClubIdEventsView,
    ClubIdLookupView,
    ClubIdOverviewView,
    CounterView,
    ReassignView,
    ReservationReleaseView,
    ReservationsView,
)
from .views_reconciliation import (
    BulkView,
    ExportView,
    InspectView,
    MissingView,
    RecheckView,
    RowActionView,
    RowsView,
    RunDetailView,
    RunsView,
)
from .views_membership import (
    ApproveView,
    BulkApproveView,
    CancelMembershipView,
    MyMembershipView,
    QueueView,
    RejectView,
    RequestMembershipView,
)
from .views_directory import (
    ExtendVerificationDeadlineView,
    ResendVerificationView,
    UserDirectoryRecipientsView,
    UserDirectorySummaryView,
    UserDirectoryView,
)
from .views_email_verification import (
    AdminMarkEmailVerifiedView,
    EmailChangeRequestView,
    EmailVerificationConfirmView,
    EmailVerificationDisputeView,
    EmailVerificationInspectView,
    EmailVerificationRequestView,
)
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
    path('logout/', LogoutView.as_view(), name='auth_logout'),
    path('token/refresh/', TokenRefreshView.as_view(), name='token_refresh'),
    path('me/', ProfileView.as_view(), name='auth_me'),
    path('users/directory/', UserDirectoryView.as_view(), name='auth_users_directory'),
    path('users/directory/summary/', UserDirectorySummaryView.as_view(), name='auth_users_directory_summary'),
    path('users/directory/recipients/', UserDirectoryRecipientsView.as_view(), name='auth_users_directory_recipients'),
    path('users/<int:pk>/verification/extend/', ExtendVerificationDeadlineView.as_view(), name='admin_extend_verification'),
    path('users/<int:pk>/verification/resend/', ResendVerificationView.as_view(), name='admin_resend_verification'),
    path('users/', UserListView.as_view(), name='auth_users_list'),
    path('users/<int:pk>/', UserDetailView.as_view(), name='auth_user_detail'),

    # Email verification: anyone can sign up; private features need a verified address (see the service module)
    path('email/verification/request/', EmailVerificationRequestView.as_view(), name='email_verification_request'),
    path('email/verification/status/', EmailVerificationInspectView.as_view(), name='email_verification_status'),
    path('email/verification/confirm/', EmailVerificationConfirmView.as_view(), name='email_verification_confirm'),
    path('email/verification/dispute/', EmailVerificationDisputeView.as_view(), name='email_verification_dispute'),
    path('email/change/', EmailChangeRequestView.as_view(), name='email_change_request'),
    path('users/<int:pk>/verify-email/', AdminMarkEmailVerifiedView.as_view(), name='admin_mark_email_verified'),

    # One-Time Cryptographic Password Setup Lifecycle (For Backup Restored Members)
    path('setup-password/request/', PasswordSetupRequestView.as_view(), name='password_setup_request'),
    path('setup-password/verify/', PasswordSetupVerifyView.as_view(), name='password_setup_verify'),
    path('setup-password/confirm/', PasswordSetupConfirmView.as_view(), name='password_setup_confirm'),

    # Member Directory Backup Import Pipeline
    path('members/import/preview/', MemberImportPreviewView.as_view(), name='member_import_preview'),
    path('members/import/commit/', MemberImportCommitView.as_view(), name='member_import_commit'),
    path('members/import/jobs/<uuid:job_id>/', MemberImportJobDetailView.as_view(), name='member_import_job_detail'),

    # Club ID Endpoints (SRKR Coding Club permanent identity)
    path('reconciliation/inspect/', InspectView.as_view(), name='reconciliation_inspect'),
    path('reconciliation/runs/', RunsView.as_view(), name='reconciliation_runs'),
    path('reconciliation/runs/<uuid:pk>/', RunDetailView.as_view(), name='reconciliation_run'),
    path('reconciliation/runs/<uuid:pk>/rows/', RowsView.as_view(), name='reconciliation_rows'),
    path('reconciliation/runs/<uuid:pk>/recheck/', RecheckView.as_view(), name='reconciliation_recheck'),
    path('reconciliation/runs/<uuid:pk>/missing/', MissingView.as_view(), name='reconciliation_missing'),
    path('reconciliation/runs/<uuid:pk>/bulk/', BulkView.as_view(), name='reconciliation_bulk'),
    path('reconciliation/runs/<uuid:pk>/export/', ExportView.as_view(), name='reconciliation_export'),
    path('reconciliation/runs/<uuid:pk>/rows/<int:row_id>/<slug:action>/', RowActionView.as_view(), name='reconciliation_row_action'),
    path('club-membership/me/', MyMembershipView.as_view(), name='club_membership_me'),
    path('club-membership/request/', RequestMembershipView.as_view(), name='club_membership_request'),
    path('club-membership/cancel/', CancelMembershipView.as_view(), name='club_membership_cancel'),
    path('club-membership/requests/', QueueView.as_view(), name='club_membership_queue'),
    path('club-membership/requests/bulk-approve/', BulkApproveView.as_view(), name='club_membership_bulk_approve'),
    path('club-membership/requests/<int:pk>/approve/', ApproveView.as_view(), name='club_membership_approve'),
    path('club-membership/requests/<int:pk>/reject/', RejectView.as_view(), name='club_membership_reject'),
    path('club-ids/overview/', ClubIdOverviewView.as_view(), name='club_ids_overview'),
    path('club-ids/lookup/', ClubIdLookupView.as_view(), name='club_ids_lookup'),
    path('club-ids/events/', ClubIdEventsView.as_view(), name='club_ids_events'),
    path('club-ids/reservations/', ReservationsView.as_view(), name='club_ids_reservations'),
    path('club-ids/reservations/<int:pk>/release/', ReservationReleaseView.as_view(), name='club_ids_reservation_release'),
    path('club-ids/allocate/', AllocateView.as_view(), name='club_ids_allocate'),
    path('club-ids/assign/', AssignView.as_view(), name='club_ids_assign'),
    path('club-ids/reassign/', ReassignView.as_view(), name='club_ids_reassign'),
    path('club-ids/clear/', ClearView.as_view(), name='club_ids_clear'),
    path('club-ids/counters/', CounterView.as_view(), name='club_ids_counters'),
    path('club-ids/next/', ClubIdNextPreviewView.as_view(), name='club_id_next'),
    path('club-ids/validate/', ClubIdValidateView.as_view(), name='club_id_validate'),

    # Referral Tracking
    path('referrals/stats/', ReferralStatsView.as_view(), name='referrals_stats'),

    # Universal Email Automation Endpoints
    path('email-templates/', EmailTemplateListCreateView.as_view(), name='email_templates_list_create'),
    path('emails/send/', EmailDispatchView.as_view(), name='emails_dispatch'),
    path('emails/jobs/<uuid:job_id>/', EmailJobDetailView.as_view(), name='email_job_detail'),
]
