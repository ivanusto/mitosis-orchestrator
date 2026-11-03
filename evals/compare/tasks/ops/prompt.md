This directory holds the setup for three sites of a small service: north-7, south-2 and east-5. Work through the steps below in that order for the sites, finishing each site before starting the next. Use only files in this directory: no network, no sudo, nothing outside it. Use python3 for anything that needs SQLite (there is no sqlite3 command line tool).

For each site, in the order north-7, south-2, east-5:

1. Render `templates/app.conf.tmpl` into `conf/<site>.conf`. Replace each `@@KEY@@` placeholder: SITE, ENV, RETENTION_DAYS, DB_PATH and LOG_PATH come from the matching keys of `params/<site>.json`; BUILD_ID is the first 12 hex characters of the sha256 of the `params/<site>.json` file; UPSTREAM_BUILD is the build id printed in the single line of the previous site's new log (see step 4), or `none` for north-7. Leave every other line as it is.
2. Create the SQLite database at the `db_path` named in `conf/<site>.conf` (create parent directories as needed). In it create `schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`, then apply every file in `migrations/` in numeric order. After each file, insert its numeric prefix (for example `001`) as `version`, with the current UTC time in ISO 8601 form as `applied_at`.
3. Import `data/<site>.csv` into the `devices` table (columns serial, model, site, qty; the first line of the file names its columns). If a serial appears more than once in the file, keep only the row with the largest qty (the earlier row if they are equal). Load all rows or none.
4. Rotate the log named by `log_path` in `conf/<site>.conf`: move it to the same name with `.1` added; create a new log at `log_path` whose only line is `rotated build=<BUILD_ID> devices=<N>`, where BUILD_ID is the build id in `conf/<site>.conf` and N is the number of rows now in that site's `devices`; write `<log_path>.1.sha256` holding the sha256 of the rotated file in sha256sum format (`<hex>  <file name without directory>`).

Then, once all three sites are done:

5. Write `out/summary.json`, a JSON object with `sites` (for each site name an object with `build_id`, `upstream_build`, `migrations` as the applied versions in order, `device_count`, `total_qty` as the sum of qty over that site's devices, and `log_sha256` as the hex digest from step 4), `total_devices` and `total_qty` over all sites.
6. Write `out/MANIFEST.sha256` with one sha256sum-format line (`<hex>  <path>`) for each of: `out/summary.json` and, for every site, `conf/<site>.conf`, the database file, the new log, the rotated log and its `.sha256` file. Paths are relative to this directory and the lines are sorted by path.
7. Run `sha256sum -c out/MANIFEST.sha256` from this directory and confirm every line says OK. Do not change any listed file afterwards.

When done, reply with each site's build id and device count.
