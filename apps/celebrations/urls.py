from django.urls import path

from . import views

app_name = 'celebrations'

urlpatterns = [
    path('pending/', views.CelebrationPendingView.as_view(), name='pending'),
    path('seen/', views.CelebrationSeenView.as_view(), name='seen'),
    path('settings/', views.CelebrationSettingsView.as_view(), name='settings'),
]
