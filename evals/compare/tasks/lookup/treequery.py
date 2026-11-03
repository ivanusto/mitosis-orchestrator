"""Static queries over the pinned tree (ast only, nothing is imported or run).

Used twice: make_questions.py picks candidates with it, and verify_key.py re-derives every answer with the
same functions, so a key can be checked against a fresh copy of the tree at any time.
"""

import ast
import os

BINOPS = {
    ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b, ast.Mult: lambda a, b: a * b,
    ast.FloorDiv: lambda a, b: a // b, ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b,
    ast.LShift: lambda a, b: a << b, ast.RShift: lambda a, b: a >> b, ast.BitOr: lambda a, b: a | b,
}


class Unresolved(Exception):
    pass


class Tree:
    def __init__(self, root):
        self.root = root
        self.files = []
        for base, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    self.files.append(os.path.relpath(os.path.join(base, name), root).replace(os.sep, "/"))
        self.files.sort()
        self._ast = {}
        self._imports = {}
        self._names = {}

    # -- modules
    @staticmethod
    def dotted(rel):
        parts = rel[:-3].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    def path_of(self, dotted):
        base = dotted.replace(".", "/")
        for cand in (base + ".py", base + "/__init__.py"):
            if cand in self.files:
                return cand
        return None

    def module(self, rel):
        if rel not in self._ast:
            with open(os.path.join(self.root, *rel.split("/")), encoding="utf-8") as fh:
                self._ast[rel] = ast.parse(fh.read(), filename=rel)
        return self._ast[rel]

    def body_statements(self, rel):
        """Top-level statements, looking inside if/try blocks too (conditional imports and definitions)."""
        out = []

        def walk(stmts):
            for st in stmts:
                out.append(st)
                if isinstance(st, ast.If):
                    walk(st.body)
                    walk(st.orelse)
                elif isinstance(st, ast.Try):
                    walk(st.body)
                    for h in st.handlers:
                        walk(h.body)
                    walk(st.orelse)
        walk(self.module(rel).body)
        return out

    # -- imports
    def imports(self, rel):
        """{local name: ('module', dotted) or ('name', dotted module, original name)}"""
        if rel in self._imports:
            return self._imports[rel]
        table = {}
        pkg = rel[:-3].split("/")[:-1]
        for st in self.body_statements(rel):
            if isinstance(st, ast.Import):
                for a in st.names:
                    if a.asname:
                        table[a.asname] = ("module", a.name)
                    else:
                        table[a.name.split(".")[0]] = ("module", a.name.split(".")[0])
            elif isinstance(st, ast.ImportFrom):
                if st.level:
                    base = pkg[: len(pkg) - (st.level - 1)] if st.level > 1 else pkg
                    mod = ".".join(base + ([st.module] if st.module else []))
                else:
                    mod = st.module or ""
                for a in st.names:
                    if a.name != "*":
                        table[a.asname or a.name] = ("name", mod, a.name)
        self._imports[rel] = table
        return table

    # -- definitions
    def assigns(self, rel):
        """{name: [value nodes]} for plain `NAME = expr` statements at module level (also inside if/try)."""
        if rel not in self._names:
            out = {}
            for st in self.body_statements(rel):
                if isinstance(st, ast.Assign) and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name):
                    out.setdefault(st.targets[0].id, []).append(st)
            self._names[rel] = out
        return self._names[rel]

    def find_def(self, rel, name, kinds=(ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        found = [st for st in self.body_statements(rel) if isinstance(st, kinds) and st.name == name]
        return found[0] if len(found) == 1 else None

    # -- name resolution
    def resolve_name(self, rel, name, depth=0):
        """Follow a name used in module rel to (module path, node kind, node). Kinds: 'assign', 'class', 'func'."""
        if depth > 6:
            raise Unresolved(name)
        node = self.find_def(rel, name, (ast.ClassDef,))
        if node is not None:
            return rel, "class", node
        node = self.find_def(rel, name, (ast.FunctionDef, ast.AsyncFunctionDef))
        if node is not None:
            return rel, "func", node
        assigns = self.assigns(rel).get(name)
        if assigns and len(assigns) == 1:
            return rel, "assign", assigns[0]
        imp = self.imports(rel).get(name)
        if imp and imp[0] == "name":
            target = self.path_of(imp[1])
            if target:
                return self.resolve_name(target, imp[2], depth + 1)
            sub = self.path_of(imp[1] + "." + imp[2])
            if sub:
                return sub, "module", None
        raise Unresolved(name)

    def resolve_attr(self, rel, node, depth=0):
        """Resolve `mod.attr` style Attribute chains to a (path, kind, node) in the tree."""
        parts = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if not isinstance(cur, ast.Name):
            raise Unresolved("attribute base")
        parts.reverse()
        imp = self.imports(rel).get(cur.id)
        if imp is None:
            raise Unresolved(cur.id)
        if imp[0] == "module":
            dotted = imp[1]
        else:
            dotted = (imp[1] + "." + imp[2]) if self.path_of(imp[1] + "." + imp[2]) else None
            if dotted is None:
                raise Unresolved(cur.id)
        for i, part in enumerate(parts[:-1]):
            nxt = dotted + "." + part
            if self.path_of(nxt):
                dotted = nxt
            else:
                raise Unresolved(part)
        target = self.path_of(dotted)
        if target is None:
            raise Unresolved(dotted)
        return self.resolve_name(target, parts[-1], depth + 1)

    # -- constants
    def evaluate(self, rel, node, touched, depth=0):
        if depth > 8:
            raise Unresolved("depth")
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, str)) and not isinstance(node.value, bool):
            return node.value
        if isinstance(node, ast.Name):
            path, kind, target = self.resolve_name(rel, node.id)
            if kind != "assign":
                raise Unresolved(node.id)
            touched.add(path)
            return self.evaluate(path, target.value, touched, depth + 1)
        if isinstance(node, ast.Attribute):
            path, kind, target = self.resolve_attr(rel, node)
            if kind != "assign":
                raise Unresolved("attr")
            touched.add(path)
            return self.evaluate(path, target.value, touched, depth + 1)
        if isinstance(node, ast.BinOp) and type(node.op) in BINOPS:
            return BINOPS[type(node.op)](self.evaluate(rel, node.left, touched, depth + 1), self.evaluate(rel, node.right, touched, depth + 1))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -self.evaluate(rel, node.operand, touched, depth + 1)
        raise Unresolved(type(node).__name__)

    def const_value(self, rel, name):
        """(value, set of module paths read) for a module-level constant; raises Unresolved."""
        assigns = self.assigns(rel).get(name)
        if not assigns or len(assigns) != 1:
            raise Unresolved(name)
        touched = {rel}
        return self.evaluate(rel, assigns[0].value, touched), touched

    # -- classes
    def class_bases(self, rel, cls):
        """Resolved base classes as (path, ClassDef); raises Unresolved for bases outside the tree (other than builtins)."""
        out = []
        for b in cls.bases:
            if isinstance(b, ast.Name) and b.id in ("object", "Exception", "BaseException", "dict", "list", "int", "str", "tuple", "set", "ValueError",
                                                  "OSError", "RuntimeError", "TypeError", "KeyError", "LookupError", "IOError", "AttributeError", "ImportError"):
                continue
            if isinstance(b, ast.Name):
                path, kind, node = self.resolve_name(rel, b.id)
            elif isinstance(b, ast.Attribute):
                path, kind, node = self.resolve_attr(rel, b)
            else:
                raise Unresolved("base")
            if kind != "class":
                raise Unresolved("base kind")
            out.append((path, node))
        return out

    def mro(self, rel, cls):
        """C3 linearisation over the classes in the tree; builtin bases are ignored."""
        def merge(seqs):
            result = []
            seqs = [list(s) for s in seqs if s]
            while seqs:
                for seq in seqs:
                    head = seq[0]
                    if not any(head in s[1:] for s in seqs):
                        break
                else:
                    raise Unresolved("inconsistent mro")
                result.append(head)
                seqs = [[x for x in s if x != head] for s in seqs]
                seqs = [s for s in seqs if s]
            return result

        def lin(path, node, depth=0):
            if depth > 10:
                raise Unresolved("depth")
            bases = self.class_bases(path, node)
            return [(path, node.name)] + merge([lin(p, n, depth + 1) for p, n in bases] + [[(p, n.name) for p, n in bases]])
        return lin(rel, cls)

    def methods(self, rel, cls):
        return {st.name: st for st in cls.body if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def method_counts(self, cls):
        counts = {}
        for st in cls.body:
            if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)):
                counts[st.name] = counts.get(st.name, 0) + 1
        return counts
