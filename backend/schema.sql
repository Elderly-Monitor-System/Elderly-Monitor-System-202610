-- 幂等建库建表


CREATE DATABASE IF NOT EXISTS fall_detector
  CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE fall_detector;

-- 设备登记表
CREATE TABLE IF NOT EXISTS devices (
  id            BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  device_id     VARCHAR(32)  NOT NULL, -- 设备唯一标识
  mac           VARCHAR(17)  NULL,
  name          VARCHAR(64)  NULL, -- 显示名称
  created_at    DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  last_seen_at  DATETIME(3)  NULL,
  UNIQUE KEY uq_device_id (device_id)
) ENGINE=InnoDB;

-- 报警事件表
CREATE TABLE IF NOT EXISTS events (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  device_id    VARCHAR(32)  NOT NULL,
  boot_id      VARCHAR(16)  NOT NULL, -- 启动会话标识
  seq          BIGINT UNSIGNED NOT NULL, -- 事件序号
  etype        ENUM('TILT','FALL','AIR') NOT NULL, -- AIR：空气超标
  up_ms        BIGINT UNSIGNED NOT NULL, -- 设备运行时间（ms）
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3), -- 后端接收时间
  dev_ts       BIGINT UNSIGNED NULL, -- 预留设备时间戳
  UNIQUE KEY uq_device_boot_seq (device_id, boot_id, seq), -- 幂等去重
  KEY idx_created_at (created_at),
  KEY idx_etype (etype)
) ENGINE=InnoDB;

-- 采样历史表（趋势曲线）
CREATE TABLE IF NOT EXISTS samples (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  device_id    VARCHAR(32)  NOT NULL,
  boot_id      VARCHAR(16)  NOT NULL,
  up_ms        BIGINT UNSIGNED NOT NULL,
  m            DECIMAL(6,3) NOT NULL, -- 加速度模值 (g)
  ang          DECIMAL(6,2) NOT NULL, -- 倾角 (度)
  air          DECIMAL(5,1) NOT NULL DEFAULT 0, -- 空气指数 0~100，越高越差
  state        VARCHAR(16)  NOT NULL,
  alarm        TINYINT(1)   NOT NULL DEFAULT 0,
  created_at   DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  UNIQUE KEY uq_device_boot_up (device_id, boot_id, up_ms), -- 幂等去重
  KEY idx_created_at (created_at)
) ENGINE=InnoDB;

-- GPS 定位；时间按北京时间存储

CREATE TABLE IF NOT EXISTS locations (
  id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
  device_id    VARCHAR(32)  NOT NULL,
  boot_id      VARCHAR(16)  NULL, -- 启动会话，仅元数据
  seq          BIGINT UNSIGNED NULL, -- 事件序号，不参与去重
  up_ms        BIGINT UNSIGNED NULL, -- 设备运行时间（ms）
  longitude    DECIMAL(10,6) NOT NULL, -- WGS84 经度
  latitude     DECIMAL(10,6) NOT NULL, -- WGS84 纬度
  gps_time     DATETIME      NOT NULL, -- GPS 定位时间（北京时间）
  sats         TINYINT       NULL, -- 卫星数（可选）
  hdop         DECIMAL(4,1)  NULL, -- HDOP（可选）
  received_at  DATETIME(3)   NOT NULL DEFAULT CURRENT_TIMESTAMP(3), -- 后端接收时间
  KEY idx_device_gps_time (device_id, gps_time), -- 定位查询索引
  KEY idx_received_at (received_at)
) ENGINE=InnoDB;
