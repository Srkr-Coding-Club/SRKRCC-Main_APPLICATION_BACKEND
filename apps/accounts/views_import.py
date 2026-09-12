import json
from django.utils import timezone
from rest_framework import status, permissions
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from apps.accounts.models import ImportJob, User
from apps.accounts.services.member_import_service import MemberImportService, MemberImportError
from apps.accounts.services.club_id_service import ClubIDService, InvalidClubIdError
from apps.accounts.services.referral_service import ReferralService
from apps.core.models import EmailTemplate, EmailJob
from apps.core.services.email_service import EmailNotificationService, TemplateSecurityError, MemberEmailContext
from apps.audit.utils import log_audit_event
from apps.core.permissions import IsAdminOrClubLead


class MemberImportPreviewView(APIView):
    """
    POST /api/admin/imports/members/preview/
    Uploads a Member Directory CSV/XLSX backup file and generates a validation preview snapshot (ImportJob).
    """
    permission_classes = [IsAdminOrClubLead]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file_obj = request.FILES.get("file")
        if not file_obj:
            return Response({"error": "No file uploaded. Please attach a .csv or .xlsx file."}, status=status.HTTP_400_BAD_REQUEST)

        custom_mapping_raw = request.data.get("mapping")
        custom_mapping = None
        if custom_mapping_raw:
            try:
                custom_mapping = json.loads(custom_mapping_raw) if isinstance(custom_mapping_raw, str) else custom_mapping_raw
            except Exception:
                pass

        idempotency_key = request.headers.get("Idempotency-Key") or request.data.get("idempotency_key")

        try:
            _, preview_data = MemberImportService.preview_import(
                file_obj=file_obj,
                filename=file_obj.name,
                user=request.user,
                custom_mapping=custom_mapping,
                idempotency_key=idempotency_key,
            )
            return Response(preview_data, status=status.HTTP_200_OK)
        except MemberImportError as mie:
            return Response({"error": str(mie)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Failed parsing backup file: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class MemberImportCommitView(APIView):
    """
    POST /api/admin/imports/members/commit/
    Commits a validated ImportJob snapshot into the database atomically with per-row savepoints.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request):
        job_id = request.data.get("job_id")
        if not job_id:
            return Response({"error": "Missing required 'job_id' parameter."}, status=status.HTTP_400_BAD_REQUEST)

        send_welcome_email = bool(request.data.get("send_welcome_email", False))
        email_template_id = request.data.get("email_template_id")

        try:
            result = MemberImportService.commit_import(
                job_id=job_id,
                user=request.user,
                send_welcome_email=send_welcome_email,
                email_template_id=email_template_id,
            )
            log_audit_event(
                actor=request.user,
                action="Committed Member Directory Import",
                target_model="ImportJob",
                target_id=str(job_id),
                details={k: v for k, v in result.items() if isinstance(v, (str, int, float, bool))},
            )
            return Response(result, status=status.HTTP_200_OK)
        except MemberImportError as mie:
            return Response({"error": str(mie)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Failed committing import: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class MemberImportJobDetailView(APIView):
    """
    GET /api/admin/imports/jobs/<uuid:job_id>/
    Retrieves the status and metadata of an import job.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, job_id):
        job = ImportJob.objects.filter(id=job_id).first()
        if not job:
            return Response({"error": "Import job not found."}, status=status.HTTP_404_NOT_FOUND)

        return Response({
            "id": str(job.id),
            "filename": job.source_filename,
            "status": job.status,
            "total_rows": job.total_rows,
            "valid_rows": job.valid_rows,
            "conflict_rows": job.conflict_rows,
            "new_users_count": job.new_users_count,
            "updated_users_count": job.updated_users_count,
            "created_at": job.created_at,
            "expires_at": job.expires_at,
        })


class ClubIdNextPreviewView(APIView):
    """
    GET /api/admin/club-ids/next/
    Previews the next sequential Club ID for the current or specified year for SRKR Coding Club.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        year = request.query_params.get("year")
        prefix = request.query_params.get("prefix", "SCC")
        target_year = int(year) if year and year.isdigit() else None

        next_id = ClubIDService.preview_next_club_id(year=target_year, prefix=prefix)
        return Response({
            "next_club_id": next_id,
            "prefix": prefix,
            "year": target_year or timezone.now().year,
        })


class ClubIdValidateView(APIView):
    """
    POST /api/admin/club-ids/validate/
    Validates format and checks if a Club ID is registered or available.
    """
    permission_classes = [permissions.IsAuthenticated]

    def post(self, request):
        club_id = (request.data.get("club_id") or "").strip()
        if not club_id:
            return Response({"error": "Missing 'club_id' parameter."}, status=status.HTTP_400_BAD_REQUEST)

        try:
            parsed = ClubIDService.parse_club_id(club_id)
            existing_user = User.objects.filter(club_id__iexact=parsed["canonical_id"]).first()
            return Response({
                "valid": True,
                "parsed": parsed,
                "is_registered": bool(existing_user),
                "member_name": f"{existing_user.first_name} {existing_user.last_name}".strip() if existing_user else None,
                "membership_status": existing_user.membership_status if existing_user else None,
            })
        except InvalidClubIdError as ice:
            return Response({"valid": False, "error": str(ice)}, status=status.HTTP_400_BAD_REQUEST)


class ReferralStatsView(APIView):
    """
    GET /api/admin/referrals/stats/
    Returns referral count and onboarded members list for the authenticated user.
    """
    permission_classes = [permissions.IsAuthenticated]

    def get(self, request):
        stats = ReferralService.get_referral_stats(request.user)
        return Response(stats)


class EmailTemplateListCreateView(APIView):
    """
    GET /api/admin/email-templates/
    POST /api/admin/email-templates/
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request):
        templates = EmailTemplate.objects.all().order_by("name")
        data = [
            {
                "id": t.id,
                "name": t.name,
                "display_title": t.display_title or t.name,
                "subject_template": t.subject_template,
                "allowed_parameters": t.allowed_parameters,
                "is_active": t.is_active,
            }
            for t in templates
        ]
        return Response(data)

    def post(self, request):
        name = request.data.get("name", "").strip().lower()
        subject = request.data.get("subject_template", "").strip()
        html = request.data.get("html_template", "").strip()
        if not name or not subject or not html:
            return Response({"error": "name, subject_template, and html_template are required."}, status=status.HTTP_400_BAD_REQUEST)

        t, _ = EmailTemplate.objects.update_or_create(
            name=name,
            defaults={
                "display_title": request.data.get("display_title", name),
                "subject_template": subject,
                "html_template": html,
                "text_template": request.data.get("text_template", ""),
                "allowed_parameters": request.data.get("allowed_parameters", ["full_name", "email", "club_id", "branch", "portal_url", "login_url"]),
                "is_active": bool(request.data.get("is_active", True)),
                "created_by": request.user,
            }
        )
        return Response({"id": t.id, "name": t.name}, status=status.HTTP_201_CREATED)


class EmailDispatchView(APIView):
    """
    POST /api/admin/emails/send/
    Dispatches a templated email campaign with recipient-level tracking.
    """
    permission_classes = [IsAdminOrClubLead]

    def post(self, request):
        template_name = request.data.get("template_name")
        template_id = request.data.get("template_id")
        recipient_emails = request.data.get("recipients", [])
        campaign_name = request.data.get("campaign_name", "")

        if template_id:
            template = EmailTemplate.objects.filter(id=template_id, is_active=True).first()
        elif template_name:
            template = EmailTemplate.objects.filter(name=template_name, is_active=True).first()
        else:
            return Response({"error": "template_name or template_id is required."}, status=status.HTTP_400_BAD_REQUEST)

        if not template and template_name:
            subject_tmpl = request.data.get("subject_template") or campaign_name or "Important Announcement from SRKR Coding Club"
            msg_body = request.data.get("message") or request.data.get("html_template") or "We have an exciting announcement for SRKR Coding Club members."
            template = EmailTemplate.objects.create(
                name=template_name,
                display_title=campaign_name or template_name,
                subject_template=subject_tmpl,
                html_template=f"<p>Hello {{{{full_name}}}},</p><p>{msg_body}</p><p>Regards,<br><strong>SRKR Coding Club Team</strong></p>",
                text_template=f"Hello {{{{full_name}}}},\n\n{msg_body}\n\nRegards,\nSRKR Coding Club Team",
                allowed_parameters=["full_name", "first_name", "last_name", "email", "club_id", "branch", "portal_url", "login_url"],
                is_active=True,
                created_by=request.user if request.user.is_authenticated else None,
            )

        if not template:
            return Response({"error": "Email template not found or inactive."}, status=status.HTTP_404_NOT_FOUND)

        if not recipient_emails or not isinstance(recipient_emails, list):
            return Response({"error": "recipients must be a non-empty list of emails."}, status=status.HTTP_400_BAD_REQUEST)

        recipient_tuples = []
        for item in recipient_emails:
            clean_email = str(item).strip().lower()
            user_obj = User.objects.filter(email__iexact=clean_email).first()
            if user_obj:
                ctx = MemberEmailContext.build_for_user(user_obj)
            else:
                ctx = {"email": clean_email, "full_name": clean_email.split("@")[0], "club_id": "N/A"}
            recipient_tuples.append((clean_email, user_obj, ctx))

        try:
            job = EmailNotificationService.create_email_job(
                template=template,
                recipient_tuples=recipient_tuples,
                campaign_name=campaign_name,
                created_by=request.user,
            )

            # Dispatch off the request/response cycle (a background thread, no
            # task queue in this app — see apps/core/tasks.py) so a large
            # campaign doesn't hold a web worker for the full SMTP send duration.
            from apps.core.tasks import process_email_job, run_in_background
            run_in_background(lambda: process_email_job(job.id))

            return Response({
                "success": True,
                "job_id": str(job.id),
                "total_recipients": job.total_recipients,
                "sent_count": job.sent_count,
                "failed_count": job.failed_count,
                "status": job.status,
            })
        except TemplateSecurityError as tse:
            return Response({"error": str(tse)}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as ex:
            return Response({"error": f"Failed dispatching email job: {str(ex)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)


class EmailJobDetailView(APIView):
    """
    GET /api/admin/emails/jobs/<uuid:job_id>/
    Retrieves the execution status and recipient-level deliveries for an email campaign job.
    """
    permission_classes = [IsAdminOrClubLead]

    def get(self, request, job_id):
        job = EmailJob.objects.filter(id=job_id).select_related('template').first()
        if not job:
            return Response({"error": "Email job not found."}, status=status.HTTP_404_NOT_FOUND)

        deliveries = job.deliveries.all()[:100]
        deliveries_data = [
            {
                "recipient_email": d.recipient_email,
                "status": d.status,
                "error_message": d.error_message,
                "sent_at": d.sent_at,
            }
            for d in deliveries
        ]

        return Response({
            "job_id": str(job.id),
            "template": job.template.name,
            "campaign_name": job.campaign_name,
            "status": job.status,
            "total_recipients": job.total_recipients,
            "sent_count": job.sent_count,
            "failed_count": job.failed_count,
            "created_at": job.created_at,
            "deliveries_sample": deliveries_data,
        })
