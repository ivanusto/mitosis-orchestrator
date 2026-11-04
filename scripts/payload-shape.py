#!/usr/bin/env python3
"""Record the shape of sanitized hook payload fixtures and show what changed between two CLI versions.

    payload-shape.py shape DIR          write DIR/SHAPE.json from every *.jsonl fixture in DIR
    payload-shape.py diff A B           compare two SHAPE.json files or version directories

A shape lists, for each mode and tool, the sorted key paths of the payload and of the hook result and
the value types seen at each path ("[]" marks list items). Values never enter it, but key names and tool
names do (a tool name is the label of its group; every key is a path segment), which is the point: a key
a new CLI adds shows up in the diff. Run it only on sanitized fixtures, where every key and tool name has
passed scripts/sanitize-payloads.py. diff prints added,
removed and type-changed paths per mode and tool, and exits 0 whatever it finds: it reports a change,
it does not judge it. Exit 2 and a one-line message mean bad input (a missing or unwritable directory, a .jsonl that is not a
file or not JSON, a file that is not a shape).
Zero dependencies; Python 3.8+.
"""

import json
import os
import sys
from typing import Any, Dict, List, Optional, Set

SHAPE_NAME = "SHAPE.json"
SHAPE_VERSION = 1


def _type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def _walk(value: Any, prefix: str, found: Dict[str, Set[str]]) -> None:
    if prefix:
        found.setdefault(prefix, set()).add(_type(value))
    if isinstance(value, dict):
        for key, item in value.items():
            _walk(item, prefix + "." + str(key) if prefix else str(key), found)
    elif isinstance(value, list):
        for item in value:
            _walk(item, prefix + "[]", found)


def _tool(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "-"
    call = payload.get("toolCall")
    if isinstance(call, dict) and isinstance(call.get("name"), str):
        return call["name"]
    name = payload.get("tool_name")
    return name if isinstance(name, str) and name else "-"


def _meta_of(directory: str) -> Dict[str, Any]:
    for name in sorted(os.listdir(directory)):
        if name.endswith(".jsonl"):
            with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
                first = fh.readline()
            try:
                head = json.loads(first)
            except ValueError:
                head = None
            meta = head.get("_meta") if isinstance(head, dict) else None
            if isinstance(meta, dict):
                return {k: meta[k] for k in ("platform", "cli_version") if k in meta}
    return {}


def build_shape(directory: str) -> Dict[str, Any]:
    if not os.path.isdir(directory):
        raise ValueError("not a directory: %s" % directory)
    raw = {}  # type: Dict[str, Dict[str, Dict[str, Set[str]]]]
    scenarios = []  # type: List[str]
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".jsonl"):
            continue
        if not os.path.isfile(os.path.join(directory, name)):
            raise ValueError("%s is not a file" % name)
        scenarios.append(name[:-len(".jsonl")])
        with open(os.path.join(directory, name), "r", encoding="utf-8") as fh:
            for number, text in enumerate(fh, 1):
                if not text.strip():
                    continue
                try:
                    obj = json.loads(text)
                except ValueError:
                    raise ValueError("%s line %d is not JSON" % (name, number))
                if not isinstance(obj, dict) or "_meta" in obj:
                    continue
                mode = str(obj.get("mode"))
                found = raw.setdefault(mode, {}).setdefault(_tool(obj.get("payload")), {})
                _walk(obj.get("payload"), "payload", found)
                _walk(obj.get("result"), "result", found)
    modes = {}  # type: Dict[str, Dict[str, Dict[str, List[str]]]]
    for mode, tools in raw.items():
        modes[mode] = {tool: {path: sorted(types) for path, types in sorted(paths.items())} for tool, paths in sorted(tools.items())}
    shape = {"shape_version": SHAPE_VERSION, "scenarios": scenarios, "modes": dict(sorted(modes.items()))}  # type: Dict[str, Any]
    shape.update(_meta_of(directory))
    return shape


def write_shape(directory: str) -> str:
    shape = build_shape(directory)
    target = os.path.join(directory, SHAPE_NAME)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(shape, indent=2, sort_keys=True) + "\n")
    return target


def load_shape(location: str) -> Dict[str, Any]:
    path = os.path.join(location, SHAPE_NAME) if os.path.isdir(location) else location
    if os.path.isdir(location) and not os.path.isfile(path):
        return build_shape(location)  # a version directory without a SHAPE.json yet
    try:
        with open(path, "r", encoding="utf-8") as fh:
            shape = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ValueError("cannot read a shape from %s: %s" % (location, exc))
    if not _shape_ok(shape):
        raise ValueError("%s is not a SHAPE.json" % location)
    return shape


def _shape_ok(shape: Any) -> bool:
    """modes -> tool -> path -> list of type names, all the way down."""
    if not isinstance(shape, dict) or not isinstance(shape.get("modes"), dict):
        return False
    for tools in shape["modes"].values():
        if not isinstance(tools, dict):
            return False
        for paths in tools.values():
            if not isinstance(paths, dict):
                return False
            for types in paths.values():
                if not isinstance(types, list) or not all(isinstance(t, str) for t in types):
                    return False
    return True


def diff_shapes(old: Dict[str, Any], new: Dict[str, Any]) -> List[str]:
    """One line per change: '<mode> <tool>: + path (types)', '- path (types)' or '~ path old -> new'."""
    lines = []  # type: List[str]
    old_modes, new_modes = old.get("modes", {}), new.get("modes", {})
    for mode in sorted(set(old_modes) | set(new_modes)):
        old_tools, new_tools = old_modes.get(mode, {}), new_modes.get(mode, {})
        for tool in sorted(set(old_tools) | set(new_tools)):
            label = "%s %s" % (mode, tool)
            if tool not in new_tools:
                lines.append("%s: - tool gone (%d paths)" % (label, len(old_tools[tool])))
                continue
            if tool not in old_tools:
                lines.append("%s: + tool new (%d paths)" % (label, len(new_tools[tool])))
                continue
            before, after = old_tools[tool], new_tools[tool]
            for path in sorted(set(after) - set(before)):
                lines.append("%s: + %s (%s)" % (label, path, "|".join(after[path])))
            for path in sorted(set(before) - set(after)):
                lines.append("%s: - %s (%s)" % (label, path, "|".join(before[path])))
            for path in sorted(set(before) & set(after)):
                if before[path] != after[path]:
                    lines.append("%s: ~ %s %s -> %s" % (label, path, "|".join(before[path]), "|".join(after[path])))
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) == 2 and args[0] == "shape":
            print(write_shape(args[1]))
            return 0
        if len(args) == 3 and args[0] == "diff":
            old, new = load_shape(args[1]), load_shape(args[2])
            lines = diff_shapes(old, new)
            for line in lines:
                print(line)
            if not lines:
                print("no shape change")
            return 0
    except (ValueError, OSError) as exc:
        sys.stderr.write("payload-shape: %s\n" % " ".join(str(exc).split()))
        return 2
    sys.stderr.write("usage: payload-shape.py shape DIR | payload-shape.py diff A B\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
