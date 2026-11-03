CREATE TABLE readings (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices (id),
    taken_at TEXT NOT NULL,
    value REAL NOT NULL
);
