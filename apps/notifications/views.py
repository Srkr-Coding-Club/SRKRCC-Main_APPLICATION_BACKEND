from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework import permissions, status
from rest_framework.response import Response as DRFResponse
from rest_framework.views import APIView

from apps.core.permissions import IsAdminOrClubLead
from apps.core.models import EmailJob
from .models import Notification
from .serializers import NotificationSerializer, BroadcastNotificationSerializer
from .services import NotificationService


class NotificationListView(APIView):
    """
    GET /api/notifications/ - List the authenticated user's notifications.
    Supports ?unread_only=true and ?limit=50.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        qs = Notification.objects.filter(recipient=request.user)
        unread_only = request.query_params.get('unread_only', '').lower() == 'true'
        if unread_only:
            qs = qs.filter(is_read=False)

        try:
            limit = min(int(request.query_params.get('limit', 50)), 100)
        except ValueError:
            limit = 50

        unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        notifications = qs[:limit]

        return DRFResponse({
            'unread_count': unread_count,
            'results': NotificationSerializer(notifications, many=True).data,
        })


class NotificationMarkAllReadView(APIView):
    """
    POST /api/notifications/mark-all-read/ - Mark all caller's notifications as read.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        count = Notification.objects.filter(
            recipient=request.user,
            is_read=False,
        ).update(is_read=True, read_at=timezone.now())

        return DRFResponse({
            'marked_count': count,
            'unread_count': 0,
        })


class NotificationDetailView(APIView):
    """
    POST /api/notifications/<id>/read/ - Mark a single notification as read.
    DELETE /api/notifications/<id>/ - Dismiss / delete a notification.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, pk):
        notif = get_object_or_404(Notification, id=pk, recipient=request.user)
        if not notif.is_read:
            notif.is_read = True
            notif.read_at = timezone.now()
            notif.save(update_fields=['is_read', 'read_at'])

        unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return DRFResponse({
            'notification': NotificationSerializer(notif).data,
            'unread_count': unread_count,
        })

    def delete(self, request, pk):
        notif = get_object_or_404(Notification, id=pk, recipient=request.user)
        notif.delete()
        unread_count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return DRFResponse({
            'deleted': True,
            'id': pk,
            'unread_count': unread_count,
        })


class NotificationUnreadCountView(APIView):
    """
    GET /api/notifications/unread-count/ - Lightweight counter for navbar bell polling.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        count = Notification.objects.filter(recipient=request.user, is_read=False).count()
        return DRFResponse({'unread_count': count})


class AdminBroadcastView(APIView):
    """
    POST /api/notifications/broadcast/ - Admin/Club Lead send in-app and/or email notifications.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request):
        serializer = BroadcastNotificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        result = NotificationService.broadcast(
            title=data['title'],
            message=data['message'],
            channels=data['channels'],
            audience=data.get('audience', 'ALL'),
            target_role=data.get('target_role', ''),
            target_hackathon_slug=data.get('target_hackathon_slug', ''),
            target_user_ids=data.get('target_user_ids', []),
            type=data.get('type', 'INFO'),
            category=data.get('category', 'GENERAL'),
            link_url=data.get('link_url', ''),
            actor=request.user,
        )

        return DRFResponse(result, status=status.HTTP_201_CREATED)


class AdminBroadcastHistoryView(APIView):
    """
    GET /api/notifications/broadcast-history/ - Recent broadcast email jobs.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        jobs = EmailJob.objects.filter(
            campaign_name__startswith='Broadcast:'
        ).order_by('-created_at')[:20]

        data = [
            {
                'id': j.id,
                'campaign_name': j.campaign_name,
                'total_recipients': j.total_recipients,
                'status': j.status,
                'created_at': j.created_at,
            }
            for j in jobs
        ]
        return DRFResponse(data)
