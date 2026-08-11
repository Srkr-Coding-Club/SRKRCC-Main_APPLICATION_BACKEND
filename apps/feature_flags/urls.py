from rest_framework.routers import DefaultRouter
from .views import FeatureFlagViewSet

router = DefaultRouter()
router.register(r'', FeatureFlagViewSet, basename='featureflag')

urlpatterns = router.urls
