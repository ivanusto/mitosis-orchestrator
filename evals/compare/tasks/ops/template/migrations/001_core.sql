CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO settings (key, value) VALUES ('site', 'UNSET');
INSERT INTO settings (key, value) VALUES ('schema_owner', 'ops');
