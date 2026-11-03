"""Question types: how a spec is worded and how its answer is derived from the tree.

A spec is a small dict, for example {"id": "q01", "type": "const_chain", "path": "asyncio/constants.py", "name": "X"}.
`answer(tree, spec)` derives the answer by ast alone; `text(spec)` words the question for prompt.md.
"""

import ast

from treequery import Unresolved


def _loc(path, node):
    return "%s:%d" % (path, node.lineno)


def _scope(tree, path, spec):
    """The class node and function node a spec points at (class may be None)."""
    cls = None
    if spec.get("cls"):
        cls = tree.find_def(path, spec["cls"], (ast.ClassDef,))
        if cls is None:
            raise Unresolved("class " + spec["cls"])
        fns = [st for st in cls.body if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == spec["func"]]
    else:
        fns = [st for st in tree.body_statements(path) if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and st.name == spec["func"]]
    if len(fns) != 1:
        raise Unresolved("function " + spec["func"])
    return cls, fns[0]


def _where(spec):
    if spec.get("cls"):
        return "method `%s.%s` in `%s`" % (spec["cls"], spec["func"], spec["path"])
    return "function `%s` in `%s`" % (spec["func"], spec["path"])


# -- const_chain
def answer_const_chain(tree, spec):
    value, touched = tree.const_value(spec["path"], spec["name"])
    if len(touched) < 2:
        raise Unresolved("single file")
    return value


def text_const_chain(spec):
    kind = "an integer" if spec["answer_kind"] == "int" else "a string"
    return ("In `%s`, `%s` is defined with an expression that uses values defined in other files of the tree. "
            "What is its value after evaluating the expression? Give %s." % (spec["path"], spec["name"], kind))


# -- imported_const
def answer_imported_const(tree, spec):
    path, kind, node = tree.resolve_name(spec["path"], spec["name"])
    if kind != "assign" or path == spec["path"]:
        raise Unresolved("not an imported constant")
    return tree.evaluate(path, node.value, {path})


def text_imported_const(spec):
    kind = "an integer" if spec["answer_kind"] == "int" else "a string"
    return ("In `%s`, the name `%s` is imported from another file of the tree. "
            "What is its value? Give %s." % (spec["path"], spec["name"], kind))


# -- default_chain
def _default_for(fn, param):
    args = fn.args
    pos = args.posonlyargs + args.args
    defaults = [None] * (len(pos) - len(args.defaults)) + list(args.defaults)
    for a, d in zip(pos, defaults):
        if a.arg == param:
            return d
    for a, d in zip(args.kwonlyargs, args.kw_defaults):
        if a.arg == param:
            return d
    return None


def answer_default_chain(tree, spec):
    _, fn = _scope(tree, spec["path"], spec)
    node = _default_for(fn, spec["param"])
    if node is None:
        raise Unresolved("no default")
    touched = {spec["path"]}
    value = tree.evaluate(spec["path"], node, touched)
    if len(touched) < 2:
        raise Unresolved("single file")
    return value


def text_default_chain(spec):
    kind = "an integer" if spec["answer_kind"] == "int" else "a string"
    return ("The default of parameter `%s` of %s is written as a name defined in another file. What is the default value? Give %s."
            % (spec["param"], _where(spec), kind))


# -- method_origin
def answer_method_origin(tree, spec):
    cls = tree.find_def(spec["path"], spec["cls"], (ast.ClassDef,))
    if cls is None:
        raise Unresolved("class")
    if spec["method"] in tree.methods(spec["path"], cls):
        raise Unresolved("defined on the class itself")
    for path, name in tree.mro(spec["path"], cls)[1:]:
        owner = tree.find_def(path, name, (ast.ClassDef,))
        counts = tree.method_counts(owner)
        if spec["method"] in counts:
            if counts[spec["method"]] != 1:
                raise Unresolved("overloaded definition")
            if path == spec["path"]:
                raise Unresolved("same file")
            return _loc(path, tree.methods(path, owner)[spec["method"]])
    raise Unresolved("not found")


def text_method_origin(spec):
    return ("Class `%s` in `%s` does not define `%s` itself. Following its base classes in the real method resolution order, "
            "give the location of the `def` line of the `%s` that its instances use." % (spec["cls"], spec["path"], spec["method"], spec["method"]))


# -- caller_callee
def _cross_file_calls(tree, spec):
    _, fn = _scope(tree, spec["path"], spec)
    found = {}
    for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
        try:
            if isinstance(call.func, ast.Name):
                path, kind, node = tree.resolve_name(spec["path"], call.func.id)
            elif isinstance(call.func, ast.Attribute):
                path, kind, node = tree.resolve_attr(spec["path"], call.func)
            else:
                continue
        except Unresolved:
            continue
        if kind == "func" and path != spec["path"]:
            found[(path, node.lineno)] = node.name
    return found


def answer_caller_callee(tree, spec):
    found = _cross_file_calls(tree, spec)
    if len(found) != 1:
        raise Unresolved("not exactly one cross-file call")
    (path, line), _ = next(iter(found.items()))
    return "%s:%d" % (path, line)


def text_caller_callee(spec):
    return ("Among the plain calls in the body of %s (a function reached through an imported name or an imported module's attribute; "
            "method calls on objects do not count), exactly one goes to a function defined in another file of the tree. "
            "Give the location of that function's `def` line." % _where(spec))


# -- raise_class
def _raised_classes(tree, spec):
    _, fn = _scope(tree, spec["path"], spec)
    found = {}
    for node in [n for n in ast.walk(fn) if isinstance(n, ast.Raise) and n.exc is not None]:
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        try:
            if isinstance(target, ast.Name):
                path, kind, cnode = tree.resolve_name(spec["path"], target.id)
            elif isinstance(target, ast.Attribute):
                path, kind, cnode = tree.resolve_attr(spec["path"], target)
            else:
                continue
        except Unresolved:
            continue
        if kind == "class":
            found[(path, cnode.lineno)] = cnode.name
    return found


def answer_raise_class(tree, spec):
    found = _raised_classes(tree, spec)
    if len(found) != 1:
        raise Unresolved("not exactly one exception class")
    (path, line), _ = next(iter(found.items()))
    if path == spec["path"]:
        raise Unresolved("same file")
    return "%s:%d" % (path, line)


def text_raise_class(spec):
    return ("The %s raises, by naming its class in a `raise` statement, exactly one kind of exception that is defined in a file of the tree, and that file is not the one the function is in. "
            "Give the location of the `class` line of that exception." % _where(spec))


TYPES = {
    "const_chain": (answer_const_chain, text_const_chain),
    "imported_const": (answer_imported_const, text_imported_const),
    "default_chain": (answer_default_chain, text_default_chain),
    "method_origin": (answer_method_origin, text_method_origin),
    "caller_callee": (answer_caller_callee, text_caller_callee),
    "raise_class": (answer_raise_class, text_raise_class),
}


def answer(tree, spec):
    return TYPES[spec["type"]][0](tree, spec)


def text(spec):
    return TYPES[spec["type"]][1](spec)
