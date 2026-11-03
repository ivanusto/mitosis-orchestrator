This directory holds a small service setup. Work through the steps below in order. Use only files in this directory: no network, no sudo, nothing outside it. Use python3 for anything that needs SQLite (there is no sqlite3 command line tool).

1. Render `templates/app.conf.tmpl` into `conf/app.conf`. Replace each `@@KEY@@` placeholder: SITE, ENV, RETENTION_DAYS and DB_PATH come from the matching keys of `params.json`; BUILD_ID is the first 12 hex characters of the sha256 of the `params.json` file. Leave every other line as it is.
2. Create the SQLite database at the `db_path` named in `conf/app.conf` (create parent directories as needed).
3. In that database create `schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)`, then apply every file in `migrations/` in numeric order. After each file, insert its numeric prefix (for example `001`) as `version`, with the current UTC time in ISO 8601 form as `applied_at`.
4. Import `data/devices.csv` into the `devices` table (columns serial, model, site, qty). Load all rows or none.
5. Rotate the log: move `logs/app.log` to `logs/app.log.1`; create a new `logs/app.log` whose only line is `rotated build=<BUILD_ID> devices=<N>`, where BUILD_ID is the build id in `conf/app.conf` and N is the number of rows now in `devices`; write `logs/app.log.1.sha256` holding the sha256 of `logs/app.log.1` in sha256sum format (`<hex>  app.log.1`).
6. Write `out/summary.json`, a JSON object with these keys: `site`, `build_id`, `migrations` (the applied versions, in order), `device_count`, `total_qty` (the sum of qty over all devices) and `log_sha256` (the hex digest from step 5).
7. Write `out/MANIFEST.sha256` with one sha256sum-format line (`<hex>  <path>`) for each of: `conf/app.conf`, the database file, `logs/app.log`, `logs/app.log.1`, `logs/app.log.1.sha256` and `out/summary.json`. Paths are relative to this directory and the lines are sorted by path.
8. Run `sha256sum -c out/MANIFEST.sha256` from this directory and confirm every line says OK. Do not change any listed file afterwards.

When done, reply with the build id and the number of devices loaded.
