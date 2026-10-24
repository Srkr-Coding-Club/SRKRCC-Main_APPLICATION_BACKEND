from django.urls import path

from apps.core.views_maintenance import MaintenanceOverviewView, MaintenanceRunView

urlpatterns = [
    path('', MaintenanceOverviewView.as_view(), name='maintenance-overview'),
    path('<slug:key>/run/', MaintenanceRunView.as_view(), name='maintenance-run'),
]
