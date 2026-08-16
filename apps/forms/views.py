from rest_framework import viewsets, permissions, status
from rest_framework.response import Response as DRFResponse
from rest_framework.pagination import PageNumberPagination
from django.utils import timezone
from .models import Form, Response
from .serializers import FormSerializer, ResponseSerializer

class StandardResultsSetPagination(PageNumberPagination):
    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

class FormViewSet(viewsets.ModelViewSet):
    queryset = Form.objects.all()
    serializer_class = FormSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    lookup_field = 'slug'

class ResponseViewSet(viewsets.ModelViewSet):
    queryset = Response.objects.all().order_by('-submitted_at')
    serializer_class = ResponseSerializer
    permission_classes = [permissions.IsAuthenticatedOrReadOnly]
    pagination_class = StandardResultsSetPagination

    def create(self, request, *args, **kwargs):
        is_test = request.query_params.get('test') == 'true'
        if is_test:
            if not request.user.is_staff:
                return DRFResponse({"error": "Test submission mode requires admin staff access."}, status=status.HTTP_403_FORBIDDEN)
            return DRFResponse({"status": "SUCCESS", "message": "Test mode simulation complete — no DB record written.", "payload": request.data}, status=status.HTTP_200_OK)

        form_id = request.data.get('form')
        if not form_id:
            return DRFResponse({"error": "form ID is required."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            form_obj = Form.objects.get(id=form_id)
        except Form.DoesNotExist:
            return DRFResponse({"error": "Form not found."}, status=status.HTTP_404_NOT_FOUND)

        # Check existing user response & edit window vs deduplication
        if request.user and request.user.is_authenticated:
            existing = Response.objects.filter(form=form_obj, user=request.user, is_test_submission=False).first()
            if existing:
                if form_obj.allow_edits_until and timezone.now() <= form_obj.allow_edits_until:
                    # Partial update of existing response within allowed edit window
                    serializer = ResponseSerializer(existing, data=request.data, partial=True, context={'form': form_obj, 'request': request})
                    serializer.is_valid(raise_exception=True)
                    serializer.save()
                    return DRFResponse(serializer.data, status=status.HTTP_200_OK)
                elif not form_obj.allow_multiple_responses:
                    return DRFResponse({"error": "You have already submitted a response for this form."}, status=status.HTTP_400_BAD_REQUEST)

        serializer = ResponseSerializer(data=request.data, context={'form': form_obj, 'request': request})
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return DRFResponse(serializer.data, status=status.HTTP_201_CREATED)
