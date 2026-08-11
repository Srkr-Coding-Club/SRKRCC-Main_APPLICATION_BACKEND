from rest_framework.routers import DefaultRouter
from .views import JobListingViewSet

router = DefaultRouter()
router.register(r'', JobListingViewSet, basename='joblisting')

urlpatterns = router.urls
