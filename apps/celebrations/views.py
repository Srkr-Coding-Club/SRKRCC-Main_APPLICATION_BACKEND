from rest_framework import permissions, serializers
from rest_framework.response import Response as DRFResponse
from rest_framework.views import APIView

from . import services
from .serializers import CelebrationSerializer


class CelebrationPendingView(APIView):
    """GET /api/celebrations/pending/ - what to celebrate for the caller now: biggest first. ``enabled`` says whether they want them."""
    permission_classes = [permissions.IsAuthenticated]
    verification_exempt = True  # the welcome comes before the email is verified

    def get(self, request):
        return DRFResponse({
            'enabled': services.is_enabled(request.user),
            'results': CelebrationSerializer(services.pending(request.user), many=True).data,
        })


class CelebrationSeenView(APIView):
    """POST /api/celebrations/seen/ {ids: [..]} - the site showed these; they will not come back."""
    permission_classes = [permissions.IsAuthenticated]
    verification_exempt = True

    def post(self, request):
        ids = request.data.get('ids')
        if not isinstance(ids, list) or len(ids) > 50:
            raise serializers.ValidationError({'ids': 'Send a list of up to 50 celebration ids.'})
        return DRFResponse({'marked': services.mark_seen(request.user, ids)})


class CelebrationSettingsView(APIView):
    """GET / PATCH /api/celebrations/settings/ {enabled} - whether the caller wants celebrations."""
    permission_classes = [permissions.IsAuthenticated]
    verification_exempt = True

    def get(self, request):
        return DRFResponse({'enabled': services.is_enabled(request.user)})

    def patch(self, request):
        enabled = request.data.get('enabled')
        if not isinstance(enabled, bool):
            raise serializers.ValidationError({'enabled': 'Send true or false.'})
        return DRFResponse({'enabled': services.set_enabled(request.user, enabled)})
