"""Logging filters binding the current request id to every record."""

from __future__ import annotations

import logging

from webapi.context import get_request_id


class RequestContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        return True
