from rest_framework.routers import DefaultRouter
from django.urls import path
from .views import EventCertificateVerifyView, EventViewSet

router = DefaultRouter()
router.register(r'', EventViewSet, basename='event')

urlpatterns = [
    path('certificates/verify/<str:code>/', EventCertificateVerifyView.as_view(), name='event-certificate-verify'),
    *router.urls,
]
