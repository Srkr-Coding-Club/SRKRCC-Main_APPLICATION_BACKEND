from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import AuditLogViewSet, AuditMonthDeleteView, AuditMonthListView

router = DefaultRouter()
router.register(r'', AuditLogViewSet, basename='auditlog')

# Fixed paths first so 'months/' is not read as an audit-log id by the router.
urlpatterns = [
    path('months/', AuditMonthListView.as_view(), name='auditlog-months'),
    path('months/<str:month>/', AuditMonthDeleteView.as_view(), name='auditlog-month-delete'),
] + router.urls
