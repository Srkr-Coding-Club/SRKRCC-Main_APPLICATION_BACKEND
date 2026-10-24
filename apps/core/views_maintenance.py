"""Admin Maintenance page: what housekeeping is due, run it now, and when it last ran. See apps.core.maintenance."""
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.audit.utils import log_audit_event
from apps.core import maintenance
from apps.core.permissions import IsAdmin


class MaintenanceOverviewView(APIView):
    """GET /api/admin/maintenance/ - every task with its state, pending work and last run."""
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(maintenance.overview())


class MaintenanceRunView(APIView):
    """POST /api/admin/maintenance/<key>/run/ - start a task in the background; the page polls the overview for the result."""
    permission_classes = [IsAdmin]

    def post(self, request, key):
        task = maintenance.get(key)
        if task is None:
            return Response({"error": "Unknown task.", "code": "UNKNOWN_TASK"}, status=status.HTTP_404_NOT_FOUND)
        reason = task.unavailable()
        if reason:
            return Response({"error": reason, "code": "TASK_UNAVAILABLE"}, status=status.HTTP_409_CONFLICT)
        try:
            run = maintenance.start_in_background(task, request.user)
        except maintenance.AlreadyRunning:
            return Response({"error": "This task is already running.", "code": "ALREADY_RUNNING"}, status=status.HTTP_409_CONFLICT)
        log_audit_event(actor=request.user, action="Started Maintenance Task", target_model="Maintenance", target_id=task.key, details={"run_id": run.pk})
        return Response(maintenance.status_of(task), status=status.HTTP_202_ACCEPTED)
