from django.urls import path
from . import views

app_name = 'notifications'

urlpatterns = [
    path('', views.NotificationListView.as_view(), name='list'),
    path('unread-count/', views.NotificationUnreadCountView.as_view(), name='unread-count'),
    path('mark-all-read/', views.NotificationMarkAllReadView.as_view(), name='mark-all-read'),
    path('<int:pk>/read/', views.NotificationDetailView.as_view(), name='read'),
    path('<int:pk>/', views.NotificationDetailView.as_view(), name='detail'),
    path('broadcast/', views.AdminBroadcastView.as_view(), name='broadcast'),
    path('broadcast-history/', views.AdminBroadcastHistoryView.as_view(), name='broadcast-history'),
]
