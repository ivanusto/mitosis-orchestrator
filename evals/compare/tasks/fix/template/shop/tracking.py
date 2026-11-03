"""Shipment tracking events and the status derived from them."""

from shop.errors import InvalidState

STAGES = ["label_created", "picked_up", "in_transit", "out_for_delivery", "delivered"]


class Tracker:
    def __init__(self, clock):
        self.clock = clock
        self._events = {}

    def record(self, tracking_no, stage, note=""):
        if stage not in STAGES:
            raise InvalidState("unknown stage: %s" % stage)
        events = self._events.setdefault(tracking_no, [])
        if events and STAGES.index(stage) < STAGES.index(events[-1]["stage"]):
            raise InvalidState("%s cannot go back from %s to %s" % (tracking_no, events[-1]["stage"], stage))
        events.append({"stage": stage, "at": self.clock.now(), "note": note})
        return events[-1]

    def status(self, tracking_no):
        events = self._events.get(tracking_no)
        return events[-1]["stage"] if events else None

    def history(self, tracking_no):
        return [(e["stage"], e["at"]) for e in self._events.get(tracking_no, [])]

    def transit_hours(self, tracking_no):
        events = self._events.get(tracking_no, [])
        starts = [e for e in events if e["stage"] == "picked_up"]
        ends = [e for e in events if e["stage"] == "delivered"]
        if not starts or not ends:
            return None
        return int((ends[-1]["at"] - starts[0]["at"]).total_seconds() // 3600)

    def stuck(self, hours):
        """Tracking numbers whose last event is older than hours and not delivered."""
        now = self.clock.now()
        return sorted(n for n, ev in self._events.items()
                      if ev[-1]["stage"] != "delivered" and (now - ev[-1]["at"]).total_seconds() >= hours * 3600)

    def progress_bps(self, tracking_no):
        status = self.status(tracking_no)
        if status is None:
            return 0
        return STAGES.index(status) * 10000 // (len(STAGES) - 1)
