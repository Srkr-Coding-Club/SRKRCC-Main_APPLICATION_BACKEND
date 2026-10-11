from django.urls import path
from rest_framework.routers import DefaultRouter
from .views import ProblemViewSet, StatsViewSet, SubmissionViewSet, UserStreakViewSet, BatchScheduleView

router = DefaultRouter()
# Register the fixed `stats` prefix before the catch-all problem route so a
# request to /stats/... is never interpreted as a problem slug lookup.
router.register(r'stats', StatsViewSet, basename='codequest-stats')
router.register(r'submissions', SubmissionViewSet, basename='codequest-submission')
router.register(r'streaks', UserStreakViewSet, basename='codequest-streak')
router.register(r'', ProblemViewSet, basename='codequest-problem')

urlpatterns = [
    path('batch-schedule/', BatchScheduleView.as_view({'post': 'create'}), name='codequest-batch-schedule'),
]

# Keep the static endpoint ahead of the router's /<slug>/ detail route so
# "batch-schedule" is never interpreted as a problem slug.
urlpatterns += router.urls
