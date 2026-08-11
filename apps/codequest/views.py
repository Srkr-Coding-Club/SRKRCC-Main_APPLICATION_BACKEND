from rest_framework import viewsets, permissions
from .models import Problem, Submission, UserStreak
from .serializers import ProblemSerializer, SubmissionSerializer, UserStreakSerializer

class ProblemViewSet(viewsets.ModelViewSet):
    queryset = Problem.objects.all()
    serializer_class = ProblemSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'slug'

class SubmissionViewSet(viewsets.ModelViewSet):
    queryset = Submission.objects.all()
    serializer_class = SubmissionSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]

class UserStreakViewSet(viewsets.ModelViewSet):
    queryset = UserStreak.objects.all()
    serializer_class = UserStreakSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
