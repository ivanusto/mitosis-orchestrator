-- Devices may only be loaded for the site recorded in settings.site.
CREATE TABLE devices (
    id INTEGER PRIMARY KEY,
    serial TEXT NOT NULL,
    model TEXT NOT NULL,
    site TEXT NOT NULL,
    qty INTEGER NOT NULL CHECK (qty >= 0)
);
CREATE TRIGGER devices_site_guard BEFORE INSERT ON devices
WHEN NEW.site <> (SELECT value FROM settings WHERE key = 'site')
BEGIN
    SELECT RAISE(ABORT, 'device site does not match settings.site');
END;
