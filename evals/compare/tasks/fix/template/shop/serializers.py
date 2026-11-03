"""Turn orders, invoices and dates into plain JSON-friendly structures and back."""

import dataclasses
import datetime
import json


def to_jsonable(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value) if isinstance(value, (set, frozenset)) else value
        return [to_jsonable(v) for v in items]
    if isinstance(value, datetime.datetime):
        return value.isoformat(timespec="seconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return value


def dumps(value):
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"))


def parse_date(text):
    return datetime.date.fromisoformat(text)


def parse_datetime(text):
    return datetime.datetime.fromisoformat(text)


def roundtrip_dates(payload, keys):
    """Convert the ISO strings under the given keys of a dict back into dates."""
    out = dict(payload)
    for key in keys:
        if isinstance(out.get(key), str):
            out[key] = parse_date(out[key])
    return out
