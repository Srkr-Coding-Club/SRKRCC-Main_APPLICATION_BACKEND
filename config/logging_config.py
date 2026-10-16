"""LOGGING and optional Sentry setup.

Logs go to stdout (what Render/Docker collect). JSON lines in production, readable text with DEBUG on.
Sentry is only initialised when SENTRY_DSN is set and sentry-sdk is installed.
"""
import json
import logging
import os


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            'time': self.formatTime(record, '%Y-%m-%dT%H:%M:%S%z'),
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
        }
        if record.exc_info:
            payload['exception'] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def build_logging(debug: bool) -> dict:
    level = os.getenv('LOG_LEVEL', 'INFO').upper()
    return {
        'version': 1,
        'disable_existing_loggers': False,
        'formatters': {
            'json': {'()': 'config.logging_config.JsonFormatter'},
            'text': {'format': '%(asctime)s %(levelname)s %(name)s: %(message)s'},
        },
        'handlers': {
            'console': {'class': 'logging.StreamHandler', 'formatter': 'text' if debug else 'json'},
        },
        'root': {'handlers': ['console'], 'level': level},
        'loggers': {
            'django.request': {'handlers': ['console'], 'level': 'WARNING', 'propagate': False},
        },
    }


def init_sentry() -> bool:
    dsn = os.getenv('SENTRY_DSN', '').strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logging.getLogger(__name__).warning('SENTRY_DSN is set but sentry-sdk is not installed; error tracking is off.')
        return False
    sentry_sdk.init(
        dsn=dsn,
        environment=os.getenv('SENTRY_ENVIRONMENT', 'production'),
        traces_sample_rate=float(os.getenv('SENTRY_TRACES_SAMPLE_RATE', '0')),
        send_default_pii=False,
    )
    return True
