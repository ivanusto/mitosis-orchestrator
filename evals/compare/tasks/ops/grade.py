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
SITES = ["north-7", "south-2", "east-5"]
MIGRATIONS = ["001_core.sql", "002_devices.sql", "003_readings.sql", "004_indexes.sql"]


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def pristine(*parts):
    return read_bytes(os.path.join(PRISTINE, *parts))


def expected_rows(site):
    """CSV rows after the duplicate rule: largest qty wins, the earlier row on a tie."""
    with open(os.path.join(PRISTINE, "data", site + ".csv"), newline="", encoding="utf-8") as fh:
        rows = [(r["serial"], r["model"], r["site"], int(r["qty"])) for r in csv.DictReader(fh)]
    best = {}
    for row in rows:
        if row[0] not in best or row[3] > best[row[0]][3]:
            best[row[0]] = row
    return sorted(best.values())


def grade(work):
    errors = []

    def check(ok, message):
        if not ok:
            errors.append(message)
        return ok

    def path(*parts):
        return os.path.join(work, *parts)

    inputs = ["templates/app.conf.tmpl"] + ["migrations/" + m for m in MIGRATIONS]
    for site in SITES:
        inputs += ["params/%s.json" % site, "data/%s.csv" % site]
    for rel in inputs:
        p = path(*rel.split("/"))
        check(os.path.isfile(p) and read_bytes(p) == pristine(*rel.split("/")), "input changed or missing: " + rel)

    template = pristine("templates", "app.conf.tmpl").decode("utf-8")
    manifest_paths = ["out/summary.json"]
    summary_want = {"sites": {}, "total_devices": 0, "total_qty": 0}
    previous_build = "none"
    for site in SITES:
        params_raw = pristine("params", site + ".json")
        params = json.loads(params_raw.decode("utf-8"))
        build_id = sha256_bytes(params_raw)[:12]
        db_rel, log_rel = params["db_path"], params["log_path"]
        tag = "[%s] " % site

        text = template
        for key, value in (("SITE", params["site"]), ("ENV", params["env"]), ("RETENTION_DAYS", str(params["retention_days"])), ("DB_PATH", db_rel),
                           ("LOG_PATH", log_rel), ("BUILD_ID", build_id), ("UPSTREAM_BUILD", previous_build)):
            text = text.replace("@@%s@@" % key, value)
        conf_path = path("conf", site + ".conf")
        check(os.path.isfile(conf_path) and read_bytes(conf_path).decode("utf-8", "replace") == text, tag + "conf differs from the expected render")

        want = expected_rows(site)
        n_dev, total_qty = len(want), sum(r[3] for r in want)
        db_path = path(*db_rel.split("/"))
        if check(os.path.isfile(db_path), tag + "database file missing"):
            con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
            try:
                names = {(r[0], r[1]) for r in con.execute("SELECT type, name FROM sqlite_master")}
                for kind, name in (("table", "settings"), ("table", "schema_migrations"), ("table", "devices"), ("table", "readings"),
                                   ("index", "idx_devices_serial"), ("index", "idx_readings_device"), ("trigger", "devices_site_guard")):
                    check((kind, name) in names, tag + "missing %s %s" % (kind, name))
                rows = list(con.execute("SELECT version, applied_at FROM schema_migrations ORDER BY rowid"))
                check([r[0] for r in rows] == ["001", "002", "003", "004"], tag + "schema_migrations versions wrong")
                check(all(re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}", r[1] or "") for r in rows), tag + "applied_at not ISO 8601")
                settings = dict(con.execute("SELECT key, value FROM settings"))
                check(settings.get("site") == site, tag + "settings.site is not the configured site")
                check(settings.get("schema_owner") == "ops", tag + "settings.schema_owner changed")
                check(sorted(con.execute("SELECT serial, model, site, qty FROM devices")) == want, tag + "devices rows differ from the expected import")
                check(con.execute("PRAGMA integrity_check").fetchone()[0] == "ok", tag + "integrity_check failed")
            except sqlite3.Error as exc:
                check(False, tag + "database unreadable: %s" % exc)
            finally:
                con.close()

        old_log, new_log = path(*log_rel.split("/")) + ".1", path(*log_rel.split("/"))
        original = pristine(*log_rel.split("/"))
        check(os.path.isfile(old_log) and read_bytes(old_log) == original, tag + "rotated log is not the original log")
        want_line = "rotated build=%s devices=%d" % (build_id, n_dev)
        check(os.path.isfile(new_log) and read_bytes(new_log).decode("utf-8", "replace").splitlines() == [want_line], tag + "new log is not the single rotation line")
        sha_file = old_log + ".sha256"
        log_hex = sha256_bytes(original)
        check(os.path.isfile(sha_file) and read_bytes(sha_file).decode("utf-8", "replace").strip() == "%s  %s" % (log_hex, os.path.basename(old_log)), tag + "sha256 file wrong")

        summary_want["sites"][site] = {"build_id": build_id, "upstream_build": previous_build, "migrations": ["001", "002", "003", "004"],
                                       "device_count": n_dev, "total_qty": total_qty, "log_sha256": log_hex}
        summary_want["total_devices"] += n_dev
        summary_want["total_qty"] += total_qty
        manifest_paths += ["conf/%s.conf" % site, db_rel, log_rel, log_rel + ".1", log_rel + ".1.sha256"]
        previous_build = build_id

    summary_path = path("out", "summary.json")
    if check(os.path.isfile(summary_path), "out/summary.json missing"):
        try:
            summary = json.loads(read_bytes(summary_path).decode("utf-8"))
            check(summary == summary_want, "out/summary.json differs from the expected summary")
        except ValueError:
            check(False, "out/summary.json is not JSON")

    manifest_path = path("out", "MANIFEST.sha256")
    if check(os.path.isfile(manifest_path), "out/MANIFEST.sha256 missing"):
        entries = []
        for ln in [ln for ln in read_bytes(manifest_path).decode("utf-8", "replace").splitlines() if ln.strip()]:
            m = re.match(r"^([0-9a-f]{64})  (.+)$", ln)
            if check(m is not None, "malformed manifest line"):
                entries.append((m.group(1), m.group(2)))
        check([p for _, p in entries] == sorted(manifest_paths), "manifest paths are not the expected sorted list")
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
