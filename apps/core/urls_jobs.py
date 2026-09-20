from django.urls import path
from apps.core.views_jobs import BackgroundJobsOverviewView

urlpatterns = [
    path('', BackgroundJobsOverviewView.as_view(), name='background-jobs-overview'),
]
