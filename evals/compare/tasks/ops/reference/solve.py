#!/usr/bin/env python3
"""Reference solution for the ops task: python3 solve.py WORKDIR.

Each step reads what the previous step produced, so a step cannot run without it.
STEPS lists them in order; run(work, skip=...) lets tests prove the dependencies.
"""

import csv
import datetime
import hashlib
import json
import os
import sqlite3
import sys


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_conf(work):
    conf = {}
    with open(os.path.join(work, "conf", "app.conf"), encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                key, _, value = line.partition("=")
                conf[key.strip()] = value.strip()
    return conf


def connect(work):
    path = os.path.join(work, read_conf(work)["db_path"])
    return sqlite3.connect("file:%s?mode=rw" % path, uri=True)  # mode=rw: never creates a missing file


def step_render(work):
    with open(os.path.join(work, "params.json"), "rb") as fh:
        raw = fh.read()
    params = json.loads(raw.decode("utf-8"))
    with open(os.path.join(work, "templates", "app.conf.tmpl"), encoding="utf-8") as fh:
        text = fh.read()
    values = {
        "SITE": params["site"], "ENV": params["env"], "RETENTION_DAYS": str(params["retention_days"]),
        "DB_PATH": params["db_path"], "BUILD_ID": hashlib.sha256(raw).hexdigest()[:12],
    }
    for key, value in values.items():
        text = text.replace("@@%s@@" % key, value)
    os.makedirs(os.path.join(work, "conf"), exist_ok=True)
    with open(os.path.join(work, "conf", "app.conf"), "w", encoding="utf-8") as fh:
        fh.write(text)


def step_create_db(work):
    path = os.path.join(work, read_conf(work)["db_path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    con.commit()
    con.close()


def step_migrate(work):
    con = connect(work)
    names = sorted(os.listdir(os.path.join(work, "migrations")), key=lambda n: int(n.split("_")[0]))
    for name in names:
        with open(os.path.join(work, "migrations", name), encoding="utf-8") as fh:
            con.executescript(fh.read())
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (name.split("_")[0], stamp))
        con.commit()
    con.close()


def step_set_site(work):
    con = connect(work)
    con.execute("UPDATE settings SET value = ? WHERE key = 'site'", (read_conf(work)["site"],))
    con.commit()
    con.close()


def step_import(work):
    con = connect(work)
    with open(os.path.join(work, "data", "devices.csv"), newline="", encoding="utf-8") as fh:
        rows = [(r["serial"], r["model"], r["site"], int(r["qty"])) for r in csv.DictReader(fh)]
    with con:
        con.executemany("INSERT INTO devices (serial, model, site, qty) VALUES (?, ?, ?, ?)", rows)
    con.close()


def step_rotate(work):
    conf = read_conf(work)
    con = connect(work)
    count = con.execute("SELECT count(*) FROM devices").fetchone()[0]
    con.close()
    if count == 0:
        raise RuntimeError("no devices imported yet")
    log = os.path.join(work, "logs", "app.log")
    os.replace(log, log + ".1")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("rotated build=%s devices=%d\n" % (conf["build_id"], count))
    with open(log + ".1.sha256", "w", encoding="utf-8") as fh:
        fh.write("%s  app.log.1\n" % sha256_file(log + ".1"))


def step_summary(work):
    conf = read_conf(work)
    con = connect(work)
    migrations = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY rowid")]
    count, qty = con.execute("SELECT count(*), coalesce(sum(qty), 0) FROM devices").fetchone()
    con.close()
    with open(os.path.join(work, "logs", "app.log.1.sha256"), encoding="utf-8") as fh:
        digest = fh.read().split()[0]
    os.makedirs(os.path.join(work, "out"), exist_ok=True)
    with open(os.path.join(work, "out", "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"site": conf["site"], "build_id": conf["build_id"], "migrations": migrations,
                   "device_count": count, "total_qty": qty, "log_sha256": digest}, fh, indent=2)
        fh.write("\n")


def manifest_paths(work):
    return sorted(["conf/app.conf", read_conf(work)["db_path"], "logs/app.log", "logs/app.log.1",
                   "logs/app.log.1.sha256", "out/summary.json"])


def step_manifest(work):
    with open(os.path.join(work, "out", "summary.json"), encoding="utf-8") as fh:
        json.load(fh)
    lines = ["%s  %s" % (sha256_file(os.path.join(work, p)), p) for p in manifest_paths(work)]
    with open(os.path.join(work, "out", "MANIFEST.sha256"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def step_verify(work):
    with open(os.path.join(work, "out", "MANIFEST.sha256"), encoding="utf-8") as fh:
        for line in fh:
            digest, _, path = line.rstrip("\n").partition("  ")
            if sha256_file(os.path.join(work, path)) != digest:
                raise RuntimeError("manifest mismatch for " + path)


STEPS = [step_render, step_create_db, step_migrate, step_set_site, step_import, step_rotate,
         step_summary, step_manifest, step_verify]


def run(work, skip=()):
    for step in STEPS:
        if step.__name__ not in skip:
            step(work)


if __name__ == "__main__":
    run(os.path.abspath(sys.argv[1]))
