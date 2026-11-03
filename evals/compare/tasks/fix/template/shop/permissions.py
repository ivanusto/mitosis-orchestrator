"""Roles and permission checks for the back office."""

from shop.errors import ShopError

ROLES = {
    "viewer": {"orders.read", "reports.read"},
    "clerk": {"orders.read", "orders.write", "inventory.read", "returns.write"},
    "manager": {"orders.read", "orders.write", "inventory.read", "inventory.write", "returns.write", "reports.read", "refunds.approve"},
    "admin": {"*"},
}


class PermissionDenied(ShopError):
    pass


class Directory:
    def __init__(self):
        self._roles = {}

    def assign(self, user, role):
        if role not in ROLES:
            raise ValueError("unknown role: %s" % role)
        self._roles.setdefault(user, set()).add(role)

    def revoke(self, user, role):
        self._roles.get(user, set()).discard(role)

    def permissions(self, user):
        granted = set()
        for role in self._roles.get(user, ()):
            granted |= ROLES[role]
        return granted

    def can(self, user, permission):
        granted = self.permissions(user)
        return "*" in granted or permission in granted

    def require(self, user, permission):
        if not self.can(user, permission):
            raise PermissionDenied("%s may not %s" % (user, permission))

    def users_with(self, permission):
        return sorted(u for u in self._roles if self.can(u, permission))
