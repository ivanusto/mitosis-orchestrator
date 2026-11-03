#!/usr/bin/env python3
"""Derive every answer from the pinned tree with ast and compare with key.json.

    python3 verify_key.py [TREE]      TREE is a directory holding asyncio/, email/, http/, logging/
                                      (default: the local standard library, checked against the manifest)
    python3 verify_key.py --write     overwrite key.json with the derived answers (review the diff)

Exit 0 when every derived answer equals the key. Nothing here uses memory of the stdlib: each answer is
found by following the names in the source, the way the question asks.
"""

import ast
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = os.path.join(HERE, "key.json")
sys.path.insert(0, HERE)
import scaffold  # noqa: E402


class Tree:
    def __init__(self, root):
        self.root = root
        self._cache = {}

    def module(self, rel):
        if rel not in self._cache:
            with open(os.path.join(self.root, *rel.split("/")), encoding="utf-8") as fh:
                self._cache[rel] = ast.parse(fh.read(), filename=rel)
        return self._cache[rel]


def evaluate(node, env):
    """Evaluate a constant expression made of literals, names from env, + * % ** and tuples."""
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        return env[node.id]
    if isinstance(node, ast.Tuple):
        return tuple(evaluate(e, env) for e in node.elts)
    if isinstance(node, ast.BinOp):
        left, right = evaluate(node.left, env), evaluate(node.right, env)
        ops = {ast.Add: lambda a, b: a + b, ast.Mult: lambda a, b: a * b, ast.Mod: lambda a, b: a % b, ast.Pow: lambda a, b: a ** b}
        return ops[type(node.op)](left, right)
    raise ValueError("unsupported expression: " + ast.dump(node))


def module_assign(tree, rel, name):
    """(value node, lineno) of the last top-level `name = ...` in a module."""
    found = None
    for node in tree.module(rel).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
            found = (node.value, node.lineno)
    if found is None:
        raise KeyError("%s: no top-level %s" % (rel, name))
    return found


def module_value(tree, rel, name):
    env = {}
    for node in tree.module(rel).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            try:
                env[node.targets[0].id] = evaluate(node.value, env)
            except (ValueError, KeyError):
                pass
    return env[name]


def find_class(tree, rel, name):
    for node in tree.module(rel).body:
        if isinstance(node, ast.ClassDef) and node.name == name:
            return node
    raise KeyError("%s: no class %s" % (rel, name))


def find_func(body_owner, name):
    for node in body_owner.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise KeyError("no function " + name)


def calls_in(func, predicate):
    return [n for n in ast.walk(func) if isinstance(n, ast.Call) and predicate(n)]


def attr_chain(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def derive(tree):
    answers = {}

    # q1: the sendfile fallback chooses its block size from a constants name; resolve it
    fallback = find_func(find_class(tree, "asyncio/base_events.py", "BaseEventLoop"), "_sock_sendfile_fallback")
    names = {attr_chain(n) for n in ast.walk(fallback) if isinstance(n, ast.Attribute)}
    assert "constants.SENDFILE_FALLBACK_READBUFFER_SIZE" in names
    answers["q1"] = module_value(tree, "asyncio/constants.py", "SENDFILE_FALLBACK_READBUFFER_SIZE")

    # q2: SimpleHTTPRequestHandler.server_version is an expression over __version__
    cls = find_class(tree, "http/server.py", "SimpleHTTPRequestHandler")
    expr = [n.value for n in cls.body if isinstance(n, ast.Assign) and n.targets[0].id == "server_version"][0]
    answers["q2"] = evaluate(expr, {"__version__": module_value(tree, "http/server.py", "__version__")})

    # q3: the message raised by _read_headers when too many header lines arrive
    func = find_func(tree.module("http/client.py"), "_read_headers")
    raises = [n for n in ast.walk(func) if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
              and attr_chain(n.exc.func) == "HTTPException"]
    assert len(raises) == 1
    answers["q3"] = evaluate(raises[0].exc.args[0], {"_MAXHEADERS": module_value(tree, "http/client.py", "_MAXHEADERS")})

    # q4: SMTPUTF8 -> SMTP -> default -> EmailPolicy -> Policy; where is max_line_length assigned?
    policy = tree.module("email/policy.py")
    smtp_chain = []
    for node in policy.body:
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id in ("default", "SMTP", "SMTPUTF8"):
            smtp_chain.append(node.targets[0].id)
            if isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Attribute):
                assert all(k.arg != "max_line_length" for k in node.value.keywords) or node.targets[0].id == "HTTP"
    assert smtp_chain == ["default", "SMTP", "SMTPUTF8"]
    email_policy = find_class(tree, "email/policy.py", "EmailPolicy")
    assert [b.id for b in email_policy.bases] == ["Policy"]
    assert not [n for n in email_policy.body if isinstance(n, ast.Assign) and n.targets[0].id == "max_line_length"]
    base = find_class(tree, "email/_policybase.py", "Policy")
    line = [n.lineno for n in base.body if isinstance(n, ast.Assign) and n.targets[0].id == "max_line_length"]
    assert len(line) == 1
    answers["q4"] = "email/_policybase.py:%d" % line[0]

    # q5: CHARSETS['utf-8'] header and body encoding constants
    charsets, _ = module_assign(tree, "email/charset.py", "CHARSETS")
    env = {n: module_value(tree, "email/charset.py", n) for n in ("QP", "BASE64", "SHORTEST")}
    entry = [v for k, v in zip(charsets.keys, charsets.values) if isinstance(k, ast.Constant) and k.value == "utf-8"][0]
    answers["q5"] = [evaluate(entry.elts[0], env), evaluate(entry.elts[1], env)]

    # q6: the function handed to atexit.register in logging/__init__.py, and its def line
    init = tree.module("logging/__init__.py")
    regs = [n.value for n in init.body if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and attr_chain(n.value.func) == "atexit.register"]
    assert len(regs) == 1 and isinstance(regs[0].args[0], ast.Name)
    answers["q6"] = "logging/__init__.py:%d" % find_func(init, regs[0].args[0].id).lineno

    # q7: default port of logging.config.listen() plus the SOAP default port in logging.handlers
    listen = find_func(tree.module("logging/config.py"), "listen")
    default_node = listen.args.defaults[0]
    port = evaluate(default_node, {"DEFAULT_LOGGING_CONFIG_PORT": module_value(tree, "logging/config.py", "DEFAULT_LOGGING_CONFIG_PORT")})
    answers["q7"] = port + module_value(tree, "logging/handlers.py", "DEFAULT_SOAP_LOGGING_PORT")

    # q8: the self method send_error calls to begin the response (status code and message), and its def line
    handler = find_class(tree, "http/server.py", "BaseHTTPRequestHandler")
    send_error = find_func(handler, "send_error")
    called = {n.func.attr for n in calls_in(send_error, lambda c: isinstance(c.func, ast.Attribute) and attr_chain(c.func).startswith("self."))}
    assert "send_response" in called
    answers["q8"] = "http/server.py:%d" % find_func(handler, "send_response").lineno
    return answers


def main(argv):
    write = "--write" in argv
    args = [a for a in argv[1:] if a != "--write"]
    if args:
        root = os.path.abspath(args[0])
    else:
        root = scaffold.stdlib_root()
        problems = scaffold.verify_tree(root)
        if problems:
            print("local stdlib does not match the manifest: %s" % problems[:3], file=sys.stderr)
            return 2
    derived = derive(Tree(root))
    if write:
        with open(KEY, "w", encoding="utf-8") as fh:
            json.dump(derived, fh, indent=1, sort_keys=True)
            fh.write("\n")
        print("wrote key.json")
        return 0
    with open(KEY, encoding="utf-8") as fh:
        key = json.load(fh)
    bad = [q for q in sorted(set(key) | set(derived)) if key.get(q) != derived.get(q)]
    for q in bad:
        print("MISMATCH %s: key=%r derived=%r" % (q, key.get(q), derived.get(q)))
    print("verify_key: %d questions, %d mismatches" % (len(derived), len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
