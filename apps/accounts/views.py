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

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except Exception as ex:
            detail = getattr(ex, 'detail', None)
            if isinstance(detail, dict) and detail.get('code'):
                code_val = detail['code']
                if isinstance(code_val, list):
                    code_val = code_val[0]
                detail_val = detail.get('detail', 'Password setup is required.')
                if isinstance(detail_val, list):
                    detail_val = detail_val[0]
                return Response(
                    {"code": str(code_val), "detail": str(detail_val)},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            raise
        return Response(serializer.validated_data, status=status.HTTP_200_OK)

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
