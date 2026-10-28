-- 旧库执行一次；新库用 schema.sql


-- 增加 AIR 事件类型
ALTER TABLE events
  MODIFY etype ENUM('TILT','FALL','AIR') NOT NULL;

-- 增加空气指数列
ALTER TABLE samples
  ADD COLUMN air DECIMAL(5,1) NOT NULL DEFAULT 0 AFTER ang;
