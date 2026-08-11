from rest_framework.routers import DefaultRouter
from .views import FormViewSet, ResponseViewSet

router = DefaultRouter()
router.register(r'submissions', ResponseViewSet, basename='form-submission')
router.register(r'', FormViewSet, basename='form')

urlpatterns = router.urls
