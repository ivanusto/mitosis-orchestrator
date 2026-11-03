#!/usr/bin/env python3
"""Reference solution for the ops task: python3 solve.py WORKDIR.

STEPS is the flat, ordered list of (label, function(work)). Every step reads what an earlier step produced, and
the first step of a site reads the rotation line of the previous site's new log, so the three sites form one chain.
The naive_* helpers do what a script copied from the first site would do; the tests use them to show where
each trap bites.
"""

import csv
import datetime
import hashlib
import json
import os
import sqlite3
import sys

SITES = ["north-7", "south-2", "east-5"]


def sha256_file(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_text(work, rel):
    with open(os.path.join(work, rel), encoding="utf-8") as fh:
        return fh.read()


def read_conf(work, site):
    conf = {}
    for line in read_text(work, "conf/%s.conf" % site).splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            conf[key.strip()] = value.strip()
    return conf


def connect(work, site):
    path = os.path.join(work, read_conf(work, site)["db_path"])
    return sqlite3.connect("file:%s?mode=rw" % path, uri=True)  # mode=rw: never creates a missing file


def upstream_build(work, site):
    index = SITES.index(site)
    if index == 0:
        return "none"
    previous = SITES[index - 1]
    first = read_text(work, read_conf(work, previous)["log_path"]).splitlines()[0]  # "rotated build=<id> devices=<n>"
    return first.split("build=")[1].split()[0]


def step_render(work, site):
    with open(os.path.join(work, "params", site + ".json"), "rb") as fh:
        raw = fh.read()
    params = json.loads(raw.decode("utf-8"))
    text = read_text(work, "templates/app.conf.tmpl")
    values = {
        "SITE": params["site"], "ENV": params["env"], "RETENTION_DAYS": str(params["retention_days"]), "DB_PATH": params["db_path"],
        "LOG_PATH": params["log_path"], "BUILD_ID": hashlib.sha256(raw).hexdigest()[:12], "UPSTREAM_BUILD": upstream_build(work, site),
    }
    for key, value in values.items():
        text = text.replace("@@%s@@" % key, value)
    os.makedirs(os.path.join(work, "conf"), exist_ok=True)
    with open(os.path.join(work, "conf", site + ".conf"), "w", encoding="utf-8") as fh:
        fh.write(text)


def step_create_db(work, site):
    path = os.path.join(work, read_conf(work, site)["db_path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    con.commit()
    con.close()


def step_migrate(work, site):
    con = connect(work, site)
    for name in sorted(os.listdir(os.path.join(work, "migrations")), key=lambda n: int(n.split("_")[0])):
        con.executescript(read_text(work, "migrations/" + name))
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        con.execute("INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)", (name.split("_")[0], stamp))
        con.commit()
    con.close()


def step_set_site(work, site):
    con = connect(work, site)
    con.execute("UPDATE settings SET value = ? WHERE key = 'site'", (read_conf(work, site)["site"],))
    con.commit()
    con.close()


def csv_rows(work, site, dedupe=True):
    with open(os.path.join(work, "data", site + ".csv"), newline="", encoding="utf-8") as fh:
        rows = [(r["serial"], r["model"], r["site"], int(r["qty"])) for r in csv.DictReader(fh)]
    if not dedupe:
        return rows
    best = {}
    order = []
    for row in rows:
        if row[0] not in best:
            best[row[0]] = row
            order.append(row[0])
        elif row[3] > best[row[0]][3]:
            best[row[0]] = row
    return [best[s] for s in order]


def step_import(work, site):
    con = connect(work, site)
    with con:
        con.executemany("INSERT INTO devices (serial, model, site, qty) VALUES (?, ?, ?, ?)", csv_rows(work, site))
    con.close()


def step_rotate(work, site):
    conf = read_conf(work, site)
    con = connect(work, site)
    count = con.execute("SELECT count(*) FROM devices").fetchone()[0]
    con.close()
    if count == 0:
        raise RuntimeError("no devices imported yet")
    log = os.path.join(work, conf["log_path"])
    os.replace(log, log + ".1")
    with open(log, "w", encoding="utf-8") as fh:
        fh.write("rotated build=%s devices=%d\n" % (conf["build_id"], count))
    with open(log + ".1.sha256", "w", encoding="utf-8") as fh:
        fh.write("%s  %s\n" % (sha256_file(log + ".1"), os.path.basename(log + ".1")))


def step_summary(work):
    sites = {}
    total_devices = total_qty = 0
    for site in SITES:
        conf = read_conf(work, site)
        con = connect(work, site)
        migrations = [r[0] for r in con.execute("SELECT version FROM schema_migrations ORDER BY rowid")]
        count, qty = con.execute("SELECT count(*), coalesce(sum(qty), 0) FROM devices").fetchone()
        con.close()
        digest = read_text(work, conf["log_path"] + ".1.sha256").split()[0]
        sites[site] = {"build_id": conf["build_id"], "upstream_build": conf["upstream_build"], "migrations": migrations,
                       "device_count": count, "total_qty": qty, "log_sha256": digest}
        total_devices += count
        total_qty += qty
    os.makedirs(os.path.join(work, "out"), exist_ok=True)
    with open(os.path.join(work, "out", "summary.json"), "w", encoding="utf-8") as fh:
        json.dump({"sites": sites, "total_devices": total_devices, "total_qty": total_qty}, fh, indent=2)
        fh.write("\n")


def manifest_paths(work):
    paths = ["out/summary.json"]
    for site in SITES:
        conf = read_conf(work, site)
        paths += ["conf/%s.conf" % site, conf["db_path"], conf["log_path"], conf["log_path"] + ".1", conf["log_path"] + ".1.sha256"]
    return sorted(paths)


def step_manifest(work):
    json.loads(read_text(work, "out/summary.json"))
    lines = ["%s  %s" % (sha256_file(os.path.join(work, p)), p) for p in manifest_paths(work)]
    with open(os.path.join(work, "out", "MANIFEST.sha256"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def step_verify(work):
    for line in read_text(work, "out/MANIFEST.sha256").splitlines():
        digest, _, path = line.partition("  ")
        if sha256_file(os.path.join(work, path)) != digest:
            raise RuntimeError("manifest mismatch for " + path)


def _site_steps(site):
    return [
        ("render " + site, lambda w, s=site: step_render(w, s)),
        ("create_db " + site, lambda w, s=site: step_create_db(w, s)),
        ("migrate " + site, lambda w, s=site: step_migrate(w, s)),
        ("set_site " + site, lambda w, s=site: step_set_site(w, s)),
        ("import " + site, lambda w, s=site: step_import(w, s)),
        ("rotate " + site, lambda w, s=site: step_rotate(w, s)),
    ]


STEPS = [step for site in SITES for step in _site_steps(site)] + [("summary", step_summary), ("manifest", step_manifest), ("verify", step_verify)]


# What a script written for the first site would do on the others.
def naive_import(work, site):
    con = connect(work, site)
    with con:
        con.executemany("INSERT INTO devices (serial, model, site, qty) VALUES (?, ?, ?, ?)", csv_rows(work, site, dedupe=False))
    con.close()


def naive_rotate(work, site):
    log = os.path.join(work, "logs", site + ".log")
    os.replace(log, log + ".1")


def run(work, skip=()):
    for label, fn in STEPS:
        if label not in skip:
            fn(work)


if __name__ == "__main__":
    run(os.path.abspath(sys.argv[1]))
