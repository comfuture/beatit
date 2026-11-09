"""Keep OAuth credentials out of Uvicorn access logs without changing requests."""

import logging


class AuthQueryRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Uvicorn passes (client, method, path-with-query, HTTP version, status).
        # Redact the record before any handlers format it, retaining useful
        # endpoint/status diagnostics and all ordinary non-auth access logging.
        arguments = record.args
        if isinstance(arguments, tuple) and len(arguments) == 5:
            full_path = arguments[2]
            if isinstance(full_path, str):
                path = full_path.partition("?")[0]
                if path == "/api/auth" or path.startswith("/api/auth/"):
                    record.args = (*arguments[:2], path, *arguments[3:])
        return True


def install_auth_query_redaction():
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, AuthQueryRedactionFilter) for item in logger.filters):
        logger.addFilter(AuthQueryRedactionFilter())
