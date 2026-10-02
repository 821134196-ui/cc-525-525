"""SQLite 持久层：班级格位、睡袋物品链、代领授权、备用袋、事件与消息。

所有表均带显式约束；业务并发正确性（唯一占用、物品链追加）由
事务 + UNIQUE 索引保证，应用层在冲突时回滚并抛 ServiceError。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 班级
CREATE TABLE IF NOT EXISTS classes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    -- 格位坐标：行/列；格位编号为 R{r}C{c}
    slot_rows   INTEGER NOT NULL CHECK (slot_rows > 0),
    slot_cols   INTEGER NOT NULL CHECK (slot_cols > 0)
);

-- 儿童（对外一律使用“儿童代号”，班级页与家长端均不暴露真实姓名）
CREATE TABLE IF NOT EXISTS children (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    -- 当前 assigned 睡袋（可空：未分配 / 使用备用袋期间）
    current_bag_id INTEGER REFERENCES sleeping_bags(id) DEFERRABLE INITIALLY DEFERRED,
    active      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS slots (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    code        TEXT NOT NULL,          -- 如 R1C03
    label       TEXT NOT NULL,          -- 展示名，如 1-03
    "row"       INTEGER NOT NULL,
    col         INTEGER NOT NULL,
    assigned_child_id INTEGER REFERENCES children(id),
    UNIQUE (class_id, code)
);

-- 睡袋（含备用袋；同一睡袋不能同时分配给两个孩子由 children.current_bag_id
-- 反向唯一索引 + assigned 视图保证，见 bag_assignments 部分唯一索引）
CREATE TABLE IF NOT EXISTS sleeping_bags (
    id          INTEGER PRIMARY KEY,
    bag_no      TEXT NOT NULL UNIQUE,   -- 标签编号，全局唯一
    material    TEXT NOT NULL,
    allergy_note TEXT NOT NULL DEFAULT '',
    is_spare    INTEGER NOT NULL DEFAULT 0,
    -- 班内储存格位；备用袋也有固定存放格
    slot_id     INTEGER REFERENCES slots(id),
    status      TEXT NOT NULL DEFAULT 'stored',
                -- stored 在校格位 / taken_home 已领回 / returned_pending 已送回待核 /
                -- in_use 正在使用（含备用袋） / damaged 污损退役 / lost 标签脱落待查
    label_ok    INTEGER NOT NULL DEFAULT 1,  -- 标签是否完好
    created_at  TEXT NOT NULL
);

-- 物品链：每次状态变更追加一条，永不更新，形成完整轨迹
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    bag_id      INTEGER NOT NULL REFERENCES sleeping_bags(id),
    child_id    INTEGER REFERENCES children(id),
    type        TEXT NOT NULL,
                -- registered 登记 / pickup 领回 / return_seen 见实送回 /
                -- mispick 错拿 / swap_bag 更换 / transfer 转班 /
                -- label_off 标签脱落 / spare_used 启用备用 / spare_back 备用归还 /
                -- monday_missing 周一未带回 / damaged 污损 / recheck 复查 /
                -- authorization_used 凭授权码领回
    note        TEXT NOT NULL DEFAULT '',
    actor       TEXT NOT NULL DEFAULT '',
    ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_bag ON events(bag_id, id);

-- 代领授权：有有效期；撤销后旧码立即失效（revoked_at 非空即拒）
CREATE TABLE IF NOT EXISTS authorizations (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    child_id    INTEGER NOT NULL REFERENCES children(id),
    proxy_name  TEXT NOT NULL,
    relation    TEXT NOT NULL DEFAULT '',
    valid_from  TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    used_count  INTEGER NOT NULL DEFAULT 0,
    revoked_at  TEXT,
    created_at  TEXT NOT NULL
);

-- 单独记录的特殊事项（家长代领、标签脱落、错拿、周一未带回、污损更换、转班）
CREATE TABLE IF NOT EXISTS incidents (
    id          INTEGER PRIMARY KEY,
    child_id    INTEGER REFERENCES children(id),
    bag_id      INTEGER REFERENCES sleeping_bags(id),
    kind        TEXT NOT NULL,
                -- proxy_pickup / label_off / mispick / monday_missing /
                -- damage_replace / transfer
    detail      TEXT NOT NULL DEFAULT '',
    resolved_at TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_child ON incidents(child_id, id);

-- 备用袋库存流水
CREATE TABLE IF NOT EXISTS spare_ledger (
    id          INTEGER PRIMARY KEY,
    bag_id      INTEGER NOT NULL REFERENCES sleeping_bags(id),
    direction   TEXT NOT NULL CHECK (direction IN ('in', 'out')),
    reason      TEXT NOT NULL DEFAULT '',
    child_id    INTEGER REFERENCES children(id),
    ts          TEXT NOT NULL
);

-- 家长联系与模拟消息
CREATE TABLE IF NOT EXISTS guardians (
    id          INTEGER PRIMARY KEY,
    child_id    INTEGER NOT NULL REFERENCES children(id),
    token       TEXT NOT NULL UNIQUE,  -- 家长端登录令牌
    name        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    child_id    INTEGER NOT NULL REFERENCES children(id),
    kind        TEXT NOT NULL,         -- pickup / return_confirm / monday_reminder / proxy / damage
    body        TEXT NOT NULL,
    adapter     TEXT NOT NULL DEFAULT 'mock',
    delivered   INTEGER NOT NULL DEFAULT 0,
    ts          TEXT NOT NULL
);

-- 标签照片（本地文件系统）记录
CREATE TABLE IF NOT EXISTS label_photos (
    id          INTEGER PRIMARY KEY,
    bag_id      INTEGER NOT NULL REFERENCES sleeping_bags(id),
    path        TEXT NOT NULL,
    event_type  TEXT NOT NULL DEFAULT 'registered',
    ts          TEXT NOT NULL
);

-- 防止同一睡袋同时分配给两个孩子：
-- 仅当 current_bag_id 非空时唯一。
CREATE UNIQUE INDEX IF NOT EXISTS idx_bag_single_owner
    ON children(current_bag_id) WHERE active = 1;
"""
