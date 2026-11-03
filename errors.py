"""Structured errors for requests the application refuses.

Each error has a stable machine-readable `code`, a user-facing `message`, an
`http_status` a future FastAPI layer can use, and optional `details`. Messages
never contain secrets or other users' data.
"""


class AppError(Exception):
    code = "app_error"
    http_status = 400

    def __init__(self, message, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(AppError):
    """The request itself is invalid (empty, too long, too many items)."""

    http_status = 422

    def __init__(self, code, message, **details):
        super().__init__(message, **details)
        self.code = code


class GenerationDisabledError(AppError):
    """The global switch is off, so no new provider calls may start."""

    code = "generation_disabled"
    http_status = 503

    def __init__(self):
        super().__init__("Generation is temporarily unavailable. You can still view existing files.")


class UsageUnavailableError(AppError):
    """Usage could not be recorded, so generation was not started (fails closed)."""

    code = "usage_unavailable"
    http_status = 503

    def __init__(self):
        super().__init__("Usage tracking is unavailable, so generation was not started. Please try again later.")


class LimitExceededError(AppError):
    """A rate limit, daily quota or budget would be exceeded, so the call was not made."""

    http_status = 429

    def __init__(self, code, message, retry_after_seconds=None, **details):
        super().__init__(message, **details)
        self.code = code
        self.retry_after_seconds = retry_after_seconds

    def to_dict(self):
        data = super().to_dict()
        if self.retry_after_seconds is not None:
            data["retry_after_seconds"] = self.retry_after_seconds
        return data


class PricingUnavailableError(AppError):
    """The cost of a paid call can't be determined, so it is refused rather than allowed unmetered."""

    code = "pricing_unavailable"
    http_status = 503


class AuthorizationRequiredError(AppError):
    """A provider function was called directly, without a request authorised by the service layer."""

    code = "authorization_required"
    http_status = 500

    def __init__(self):
        super().__init__("Generation must be started through the service layer (services.py), "
                         "which enforces limits and records usage.")
