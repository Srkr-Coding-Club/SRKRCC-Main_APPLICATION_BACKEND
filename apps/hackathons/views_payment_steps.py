"""Payment steps over the API: organizers set them up and check payments; the team leader pays. See payment_steps.py."""
from __future__ import annotations

from rest_framework import permissions, status
from rest_framework.response import Response as DRFResponse
from rest_framework.views import APIView

from apps.core.throttling import ScopedRateThrottle

from . import idea_files, payment_steps
from .views import _get_hackathon


def _upload_reply(issued: dict) -> dict:
    return {'file': idea_files.serialize(issued['file']), 'post': issued['post'], 'max_bytes': issued['max_bytes']}


class PaymentStepsView(APIView):
    """GET /api/hackathons/{slug}/payment-steps/ (teams) - every step with its counts. POST (settings) - add a step."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        return DRFResponse(payment_steps.list_steps(_get_hackathon(request, slug), request.user))

    def post(self, request, slug):
        step = payment_steps.create_step(_get_hackathon(request, slug), request.user, request.data)
        return DRFResponse(payment_steps.serialize_step(step), status=status.HTTP_201_CREATED)


class PaymentStepDetailView(APIView):
    """PATCH / DELETE /api/hackathons/{slug}/payment-steps/{id}/ (settings). A step with payments is switched off, not deleted."""
    permission_classes = [permissions.IsAuthenticated]

    def patch(self, request, slug, pk):
        step = payment_steps.update_step(_get_hackathon(request, slug), request.user, pk, request.data)
        return DRFResponse(payment_steps.serialize_step(step))

    def delete(self, request, slug, pk):
        return DRFResponse({'outcome': payment_steps.delete_step(_get_hackathon(request, slug), request.user, pk)})


class PaymentStepQueueView(APIView):
    """GET /api/hackathons/{slug}/payment-steps/{id}/queue/ (teams) - the teams that have to pay, with their payments."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug, pk):
        return DRFResponse(payment_steps.queue(_get_hackathon(request, slug), request.user, pk))


class PaymentStepRemindView(APIView):
    """POST /api/hackathons/{slug}/payment-steps/{id}/remind/ (teams) - notify and email the teams that have not paid."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk):
        return DRFResponse({'reminded': payment_steps.remind_unpaid(_get_hackathon(request, slug), request.user, pk)})


class PaymentStepQrView(APIView):
    """POST /api/hackathons/{slug}/payment-steps/{id}/qr/ {name, size} - a signed upload; DELETE - remove the QR (settings).
    GET .../qr/link/ is in PaymentStepQrLinkView."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def post(self, request, slug, pk):
        issued = payment_steps.request_qr_upload(_get_hackathon(request, slug), request.user, pk, request.data.get('name'), request.data.get('size'))
        return DRFResponse(_upload_reply(issued), status=status.HTTP_201_CREATED)

    def delete(self, request, slug, pk):
        return DRFResponse(payment_steps.serialize_step(payment_steps.remove_qr(_get_hackathon(request, slug), request.user, pk)))


class PaymentStepQrFileView(APIView):
    """POST /api/hackathons/{slug}/payment-steps/{id}/qr/{file_id}/ - confirm the upload and make it the step's QR code."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk, file_id):
        step = payment_steps.complete_qr_upload(_get_hackathon(request, slug), request.user, pk, file_id)
        return DRFResponse(payment_steps.serialize_step(step))


class PaymentStepQrLinkView(APIView):
    """GET /api/hackathons/{slug}/payment-steps/{id}/qr/link/ - a short link to the QR: organizers and teams that have to pay."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug, pk):
        return DRFResponse(payment_steps.qr_link(_get_hackathon(request, slug), request.user, pk))


class MyPaymentStepsView(APIView):
    """GET /api/hackathons/{slug}/my-payment-steps/ - what the person's team has to pay, how, and where it stands."""
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request, slug):
        return DRFResponse(payment_steps.my_steps(_get_hackathon(request, slug), request.user))


class MyStepPaymentFilesView(APIView):
    """POST /api/hackathons/{slug}/payment-steps/{id}/files/ {name, size} - the leader asks for a signed upload."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def post(self, request, slug, pk):
        issued = payment_steps.request_upload(_get_hackathon(request, slug), pk, request.user, request.data.get('name'), request.data.get('size'))
        return DRFResponse(_upload_reply(issued), status=status.HTTP_201_CREATED)


class MyStepPaymentFileView(APIView):
    """POST /api/hackathons/{slug}/payment-steps/{id}/files/{file_id}/ - confirm the screenshot reached storage."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk, file_id):
        file = payment_steps.complete_upload(_get_hackathon(request, slug), pk, request.user, file_id)
        return DRFResponse({'file': idea_files.serialize(file)})


class PayStepView(APIView):
    """POST /api/hackathons/{slug}/payment-steps/{id}/pay/ {transaction_id, payment_file_id} - the leader submits the payment."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk):
        hackathon = _get_hackathon(request, slug)
        payment_steps.pay(hackathon, pk, request.user, request.data)
        return DRFResponse(payment_steps.my_steps(hackathon, request.user), status=status.HTTP_201_CREATED)


class StepPaymentActionView(APIView):
    """POST /api/hackathons/{slug}/step-payments/{id}/{verify|reject}/ (teams). Reject needs a reason the leader sees."""
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request, slug, pk, action):
        hackathon = _get_hackathon(request, slug)
        if action == 'verify':
            payment = payment_steps.verify(hackathon, request.user, pk)
        elif action == 'reject':
            payment = payment_steps.reject(hackathon, request.user, pk, request.data.get('reason', ''))
        else:
            return DRFResponse({'detail': 'Unknown action.'}, status=status.HTTP_404_NOT_FOUND)
        return DRFResponse(payment_steps.queue(hackathon, request.user, payment.step_id))


class StepPaymentScreenshotView(APIView):
    """GET /api/hackathons/{slug}/step-payments/{id}/screenshot/?inline=1 - a 60-second link: organizers and the leader."""
    permission_classes = [permissions.IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = 'form_upload'

    def get(self, request, slug, pk):
        return DRFResponse(payment_steps.screenshot_link(
            _get_hackathon(request, slug), request.user, pk, inline=request.query_params.get('inline') == '1',
        ))
