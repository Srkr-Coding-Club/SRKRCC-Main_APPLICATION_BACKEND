from rest_framework.routers import DefaultRouter
from .views import BlogPostViewSet

router = DefaultRouter()
router.register(r'', BlogPostViewSet, basename='blogpost')

urlpatterns = router.urls
