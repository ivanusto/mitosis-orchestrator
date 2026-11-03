"""A minimal publish/subscribe bus used to decouple services from notifications and audit."""


class EventBus:
    def __init__(self):
        self._handlers = {}
        self._history = []

    def subscribe(self, topic, handler):
        self._handlers.setdefault(topic, []).append(handler)
        return handler

    def unsubscribe(self, topic, handler):
        handlers = self._handlers.get(topic, [])
        if handler in handlers:
            handlers.remove(handler)
            return True
        return False

    def publish(self, topic, **payload):
        """Call every handler of topic with the payload; returns how many ran."""
        self._history.append((topic, dict(payload)))
        ran = 0
        for handler in list(self._handlers.get(topic, [])):
            handler(**payload)
            ran += 1
        return ran

    def history(self, topic=None):
        return [(t, p) for t, p in self._history if topic is None or t == topic]

    def topics(self):
        return sorted(self._handlers)
