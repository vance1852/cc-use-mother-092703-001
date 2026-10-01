"""批次监管服务的可观察错误。"""

from __future__ import annotations


class BatchError(RuntimeError):
    code = "batch_error"
    status = 400


class NotFound(BatchError):
    code = "not_found"
    status = 404


class Conflict(BatchError):
    code = "conflict"
    status = 409


class Forbidden(BatchError):
    code = "forbidden"
    status = 403


class InvalidState(BatchError):
    code = "invalid_state"
    status = 409


class ValidationFailed(BatchError):
    code = "validation_failed"
    status = 422
