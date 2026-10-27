from django.urls import path

from .views import (
    AttendanceReportExportView,
    AttendanceReportView,
    AttendanceScanView,
    AttendanceSessionListView,
    MyAttendanceRecordView,
    MyBadgeView,
)

# Mounted at 'api/' in config/urls.py, so these resolve to:
#   /api/forms/<form_id>/attendance/sessions/
#   /api/forms/<form_id>/attendance/my-badge/
#   /api/forms/<form_id>/attendance/my-record/
#   /api/forms/<form_id>/attendance/report/
#   /api/forms/<form_id>/attendance/report/export/
#   /api/attendance/scan/
urlpatterns = [
    path('forms/<int:form_id>/attendance/sessions/', AttendanceSessionListView.as_view(), name='attendance-sessions'),
    path('forms/<int:form_id>/attendance/my-badge/', MyBadgeView.as_view(), name='attendance-my-badge'),
    path('forms/<int:form_id>/attendance/my-record/', MyAttendanceRecordView.as_view(), name='attendance-my-record'),
    path('forms/<int:form_id>/attendance/report/', AttendanceReportView.as_view(), name='attendance-report'),
    path('forms/<int:form_id>/attendance/report/export/', AttendanceReportExportView.as_view(), name='attendance-report-export'),
    path('attendance/scan/', AttendanceScanView.as_view(), name='attendance-scan'),
]
