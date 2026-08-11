from rest_framework.routers import DefaultRouter
from .views import HackathonViewSet, TeamViewSet, SubmissionViewSet

router = DefaultRouter()
router.register(r'teams', TeamViewSet, basename='hackathon-team')
router.register(r'submissions', SubmissionViewSet, basename='hackathon-submission')
router.register(r'', HackathonViewSet, basename='hackathon')

urlpatterns = router.urls
