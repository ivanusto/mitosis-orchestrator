"""Small argument checks used at the edges of services."""

from shop.errors import ValidationError


def require_positive_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError("%s must be a positive integer, got %r" % (name, value))
    return value


def require_non_negative_int(value, name):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValidationError("%s must be a non-negative integer, got %r" % (name, value))
    return value


def require_text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string" % name)
    return value.strip()


def require_in(value, allowed, name):
    if value not in allowed:
        raise ValidationError("%s must be one of %s, got %r" % (name, sorted(allowed), value))
    return value


def require_email(value):
    text = require_text(value, "email")
    local, _, domain = text.partition("@")
    if not local or "." not in domain:
        raise ValidationError("not an email address: %r" % (value,))
    return text.lower()
