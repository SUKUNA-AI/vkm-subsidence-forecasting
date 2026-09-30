from __future__ import annotations

class ToolFailure(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False, details: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.details = code, message, retryable, details or {}

    def as_dict(self) -> dict:
        return {'code': self.code, 'message': self.message, 'retryable': self.retryable, 'details': self.details}
