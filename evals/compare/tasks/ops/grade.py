#!/usr/bin/env python3
"""Grade the ops task: python3 grade.py WORKDIR. Exit 0 only if the final state is exactly as asserted.

Expected values are computed from the pristine inputs kept next to this file, never from WORKDIR.
Prints one line per failed assertion and nothing else that is sensitive.
"""

import csv
import hashlib
import json
import os
import re
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PRISTINE = os.path.join(HERE, "template")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def pristine(*parts):
    return read_bytes(os.path.join(PRISTINE, *parts))


def grade(work):
    errors = []

    def check(ok, message):
        if not ok:
            errors.append(message)
        return ok

    def path(*parts):
        return os.path.join(work, *parts)

    # inputs untouched
    for rel in ("templates/app.conf.tmpl", "params.json", "data/devices.csv", "migrations/001_core.sql",
                "migrations/002_devices.sql", "migrations/003_readings.sql", "migrations/004_indexes.sql"):
        p = path(*rel.split("/"))
        check(os.path.isfile(p) and read_bytes(p) == pristine(*rel.split("/")), "input changed or missing: " + rel)

    params_raw = pristine("params.json")
    params = json.loads(params_raw.decode("utf-8"))
    build_id = sha256_bytes(params_raw)[:12]
    site = params["site"]
    db_rel = params["db_path"]

    # step 1: config
    text = pristine("templates", "app.conf.tmpl").decode("utf-8")
    for key, value in (("SITE", site), ("ENV", params["env"]), ("RETENTION_DAYS", str(params["retention_days"])),
                       ("DB_PATH", db_rel), ("BUILD_ID", build_id)):
        text = text.replace("@@%s@@" % key, value)
    conf_path = path("conf", "app.conf")
    check(os.path.isfile(conf_path) and read_bytes(conf_path).decode("utf-8", "replace") == text, "conf/app.conf differs from the expected render")

    # steps 2 to 4: database
    with open(os.path.join(PRISTINE, "data", "devices.csv"), newline="", encoding="utf-8") as fh:
        want = sorted((r["serial"], r["model"], r["site"], int(r["qty"])) for r in csv.DictReader(fh))
    n_dev, total_qty = len(want), sum(r[3] for r in want)
    db_path = path(*db_rel.split("/"))
    if check(os.path.isfile(db_path), "database file missing"):
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            names = {(r[0], r[1]) for r in con.execute("SELECT type, name FROM sqlite_master")}
            for kind, name in (("table", "settings"), ("table", "schema_migrations"), ("table", "devices"), ("table", "readings"),
                               ("index", "idx_devices_serial"), ("index", "idx_readings_device"), ("trigger", "devices_site_guard")):
                check((kind, name) in names, "missing %s %s" % (kind, name))
            rows = list(con.execute("SELECT version, applied_at FROM schema_migrations ORDER BY rowid"))
            check([r[0] for r in rows] == ["001", "002", "003", "004"], "schema_migrations versions wrong")
            check(all(re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", r[1] or "") for r in rows), "applied_at not ISO 8601")
            settings = dict(con.execute("SELECT key, value FROM settings"))
            check(settings.get("site") == site, "settings.site is not the configured site")
            check(settings.get("schema_owner") == "ops", "settings.schema_owner changed")
            got = sorted(con.execute("SELECT serial, model, site, qty FROM devices"))
            check(got == want, "devices rows differ from data/devices.csv")
            check(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_check failed")
        except sqlite3.Error as exc:
            check(False, "database unreadable: %s" % exc)
        finally:
            con.close()

    # step 5: log rotation
    old_log = path("logs", "app.log.1")
    old_expected = pristine("logs", "app.log")
    check(os.path.isfile(old_log) and read_bytes(old_log) == old_expected, "logs/app.log.1 is not the original log")
    new_log = path("logs", "app.log")
    want_line = "rotated build=%s devices=%d" % (build_id, n_dev)
    check(os.path.isfile(new_log) and read_bytes(new_log).decode("utf-8", "replace").splitlines() == [want_line], "logs/app.log is not the single rotation line")
    sha_file = path("logs", "app.log.1.sha256")
    log_hex = sha256_bytes(old_expected)
    check(os.path.isfile(sha_file) and read_bytes(sha_file).decode("utf-8", "replace").strip() == "%s  app.log.1" % log_hex, "logs/app.log.1.sha256 wrong")

    # step 6: summary
    summary_path = path("out", "summary.json")
    summary = None
    if check(os.path.isfile(summary_path), "out/summary.json missing"):
        try:
            summary = json.loads(read_bytes(summary_path).decode("utf-8"))
        except ValueError:
            check(False, "out/summary.json is not JSON")
    if summary is not None:
        expected = {"site": site, "build_id": build_id, "migrations": ["001", "002", "003", "004"],
                    "device_count": n_dev, "total_qty": total_qty, "log_sha256": log_hex}
        for key, value in expected.items():
            check(summary.get(key) == value, "summary.%s wrong" % key)

    # step 7 and 8: manifest
    manifest_path = path("out", "MANIFEST.sha256")
    if check(os.path.isfile(manifest_path), "out/MANIFEST.sha256 missing"):
        lines = [ln for ln in read_bytes(manifest_path).decode("utf-8", "replace").splitlines() if ln.strip()]
        entries = []
        for ln in lines:
            m = re.match(r"^([0-9a-f]{64})  (.+)$", ln)
            if not check(m is not None, "malformed manifest line"):
                continue
            entries.append((m.group(1), m.group(2)))
        want_paths = sorted(["conf/app.conf", db_rel, "logs/app.log", "logs/app.log.1", "logs/app.log.1.sha256", "out/summary.json"])
        check([p for _, p in entries] == want_paths, "manifest paths are not the expected sorted list")
        for digest, rel in entries:
            target = path(*rel.split("/"))
            check(os.path.isfile(target) and sha256_bytes(read_bytes(target)) == digest, "manifest hash does not match: " + rel)
    return errors


def main(argv):
    if len(argv) != 2:
        print("usage: grade.py WORKDIR", file=sys.stderr)
        return 2
    errors = grade(os.path.abspath(argv[1]))
    for e in errors:
        print("FAIL " + e)
    print("ops: %d problem(s)" % len(errors))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
