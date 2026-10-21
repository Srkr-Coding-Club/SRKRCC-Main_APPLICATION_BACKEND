from rest_framework.exceptions import APIException


class HackathonError(APIException):
    """400 with a stable machine-readable ``code`` the frontend can branch on."""
    status_code = 400
    default_code = 'HACKATHON_ERROR'

    def __init__(self, message, code='HACKATHON_ERROR', field=None, status=None, **extra):
        detail = {'detail': message, 'code': code, **extra}
        if field:
            detail['field'] = field
        self.detail = detail
        if status:
            self.status_code = status
