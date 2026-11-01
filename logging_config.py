"""Standard-library logging setup plus secret redaction.

redact() is also used on error text shown to users, so a provider error that
echoes a key or an Authorization header can't leak it.
"""
import logging
import os
import re

SECRET_ENV_VARS = ("API_KEY", "HF_TOKEN")
REDACTED = "[REDACTED]"

# Secrets we recognise by shape, in case an error message contains one we don't hold.
_PATTERNS = [
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bhf_[A-Za-z0-9]{8,}"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"(?i)((?:authorization|x-goog-api-key|x-api-key|api[-_]?key|token)[\"']?\s*[:=]\s*[\"']?)[^\s,\"'&]+"),
]


def redact(text) -> str:
    """Return text with credential values and key-shaped strings replaced."""
    text = str(text)
    for name in SECRET_ENV_VARS:
        value = (os.environ.get(name) or "").strip()
        if len(value) >= 6:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub secrets from every record, including formatted arguments."""

    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def configure_logging(level=logging.INFO):
    """Log to stderr at `level` with redaction. Safe to call more than once."""
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(RedactingFilter())
    root = logging.getLogger()
    root.handlers = [h for h in root.handlers if not getattr(h, "_prompt_expander", False)]
    handler._prompt_expander = True
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs full request URLs at INFO; keep it quiet by default
    logging.getLogger("httpx").setLevel(logging.WARNING)
