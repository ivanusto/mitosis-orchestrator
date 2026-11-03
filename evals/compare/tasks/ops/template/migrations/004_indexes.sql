CREATE UNIQUE INDEX idx_devices_serial ON devices (serial);
CREATE INDEX idx_readings_device ON readings (device_id);
