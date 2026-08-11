from rest_framework.routers import DefaultRouter
from .views import ProblemViewSet, SubmissionViewSet, UserStreakViewSet

router = DefaultRouter()
router.register(r'submissions', SubmissionViewSet, basename='codequest-submission')
router.register(r'streaks', UserStreakViewSet, basename='codequest-streak')
router.register(r'', ProblemViewSet, basename='codequest-problem')

urlpatterns = router.urls
