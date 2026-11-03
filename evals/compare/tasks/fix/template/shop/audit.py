"""Append-only audit trail of what happened to orders."""


class AuditLog:
    def __init__(self, clock):
        self.clock = clock
        self._entries = []

    def record(self, kind, subject, detail=""):
        entry = {"seq": len(self._entries) + 1, "at": self.clock.now(), "kind": kind, "subject": subject, "detail": detail}
        self._entries.append(entry)
        return entry

    def entries(self, kind=None, subject=None):
        out = self._entries
        if kind is not None:
            out = [e for e in out if e["kind"] == kind]
        if subject is not None:
            out = [e for e in out if e["subject"] == subject]
        return list(out)

    def last(self, kind=None):
        found = self.entries(kind=kind)
        return found[-1] if found else None

    def count_by_kind(self):
        counts = {}
        for entry in self._entries:
            counts[entry["kind"]] = counts.get(entry["kind"], 0) + 1
        return dict(sorted(counts.items()))
