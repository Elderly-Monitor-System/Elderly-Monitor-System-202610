# MySQL 连接池与幂等建表
import os
import threading

import pymysql
from dbutils.pooled_db import PooledDB

import config

_lock = threading.Lock()
pool = None


def _raw_connect():
    """建库连接，不指定数据库"""
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
    )


def init_schema():
    """幂等创建数据库和表"""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    conn = _raw_connect()
    try:
        cur = conn.cursor()
        # 按分号逐条执行建表语句
        for stmt in sql.split(";"):
            stmt = stmt.strip()
            if stmt:
                cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def get_pool():
    global pool
    with _lock:
        if pool is None:
            pool = PooledDB(
                creator=pymysql,
                maxconnections=10,
                mincached=1,
                host=config.DB_HOST,
                port=config.DB_PORT,
                user=config.DB_USER,
                password=config.DB_PASSWORD,
                database=config.DB_NAME,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=True,
            )
    return pool


def init():
    """初始化表结构与连接池"""
    init_schema()
    return get_pool()
