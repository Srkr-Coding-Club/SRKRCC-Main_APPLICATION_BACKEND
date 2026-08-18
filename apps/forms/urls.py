from rest_framework.routers import DefaultRouter
from .views import FormViewSet, ResponseViewSet, MemberViewSet

router = DefaultRouter()
router.register(r'submissions', ResponseViewSet, basename='form-submission')
router.register(r'', FormViewSet, basename='form')

# Separate router for /api/members/ — registered independently in config/urls.py
members_router = DefaultRouter()
members_router.register(r'', MemberViewSet, basename='member')

urlpatterns = router.urls
members_urlpatterns = members_router.urls
