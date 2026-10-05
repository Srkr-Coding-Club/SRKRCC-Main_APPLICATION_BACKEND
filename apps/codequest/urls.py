from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import ProblemViewSet, SubmissionViewSet, UserStreakViewSet, BatchScheduleView

router = DefaultRouter()
router.register(r'submissions', SubmissionViewSet, basename='codequest-submission')
router.register(r'streaks', UserStreakViewSet, basename='codequest-streak')
router.register(r'', ProblemViewSet, basename='codequest-problem')

urlpatterns = [
    path('batch-schedule/', BatchScheduleView.as_view({'post': 'create'}), name='codequest-batch-schedule'),
]

# Keep the static endpoint ahead of the router's /<slug>/ detail route so
# "batch-schedule" is never interpreted as a problem slug.
urlpatterns += router.urls
