from rest_framework import generics, permissions, status, filters
from rest_framework.response import Response
from rest_framework_simplejwt.views import TokenObtainPairView
from django.contrib.auth import get_user_model
from .serializers import (
    UserSerializer,
    UserProfileDetailSerializer,
    RegisterSerializer,
    CustomTokenObtainPairSerializer,
)

User = get_user_model()

class CustomTokenObtainPairView(TokenObtainPairView):
    """
    Custom login view returning access token, refresh token, and user profile data.
    """
    serializer_class = CustomTokenObtainPairSerializer

class UserListView(generics.ListCreateAPIView):
    queryset = User.objects.all().order_by('-created_at')
    serializer_class = UserSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    filter_backends = [filters.SearchFilter, filters.OrderingFilter]
    search_fields = [
        'club_id',
        'email',
        'first_name',
        'last_name',
        'branch',
        'phone_number',
        'roll_number',
        'referred_by_raw',
    ]
    ordering_fields = ['created_at', 'registered_at', 'club_id', 'first_name', 'email']

class RegisterView(generics.CreateAPIView):
    queryset = User.objects.all()
    permission_classes = [permissions.AllowAny]
    serializer_class = RegisterSerializer

class ProfileView(generics.RetrieveUpdateAPIView):
    serializer_class = UserProfileDetailSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_object(self):
        return self.request.user
