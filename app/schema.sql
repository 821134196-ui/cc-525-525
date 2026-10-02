-- 幼儿园午睡袋管理 · SQLite 结构
-- 物品链 = sleeping_bags(物品本体) + assignments(当前归属) + bag_events(完整事件链)

PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS classes (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 班级格位：格位号在班内唯一；一个格位当前只放一个在园睡袋（活动分配）
CREATE TABLE IF NOT EXISTS cubby_slots (
    id          INTEGER PRIMARY KEY,
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    slot_no     TEXT NOT NULL,
    UNIQUE(class_id, slot_no)
);

CREATE TABLE IF NOT EXISTS children (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,          -- 儿童代号（页面展示用，避免全名）
    name        TEXT NOT NULL,                 -- 实名仅教师侧详情可见
    class_id    INTEGER NOT NULL REFERENCES classes(id),
    allergy_note TEXT NOT NULL DEFAULT '',     -- 过敏注意事项
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS parents (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL,
    phone       TEXT NOT NULL UNIQUE,          -- 家长端登录标识（模拟扫码）
    pickup_code TEXT NOT NULL UNIQUE,          -- 家长本人领回码（扫码出示）
    child_id    INTEGER NOT NULL REFERENCES children(id)
);

-- 睡袋（物品本体）。编号全库唯一；软删除用 retired
CREATE TABLE IF NOT EXISTS sleeping_bags (
    id           INTEGER PRIMARY KEY,
    bag_no       TEXT NOT NULL UNIQUE,         -- 睡袋编号（标签上的号）
    material     TEXT NOT NULL DEFAULT '',     -- 材质（棉/羽绒/抓绒…，涉及过敏）
    allergy_note TEXT NOT NULL DEFAULT '',     -- 该睡袋相关过敏注意
    spare        INTEGER NOT NULL DEFAULT 0,   -- 1=备用睡袋（库存）
    retired      INTEGER NOT NULL DEFAULT 0,   -- 1=污损报废，退出流通
    retire_reason TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 当前有效分配（活动链头）。部分唯一索引保证同一睡袋不能同时分给两个孩子
CREATE TABLE IF NOT EXISTS assignments (
    id           INTEGER PRIMARY KEY,
    bag_id       INTEGER NOT NULL REFERENCES sleeping_bags(id),
    child_id     INTEGER NOT NULL REFERENCES children(id),
    slot_id      INTEGER REFERENCES cubby_slots(id),  -- 在园时的存放格位
    status       TEXT NOT NULL CHECK(status IN ('at_school','taken_home','spare_in_use')),
    label_damaged INTEGER NOT NULL DEFAULT 0,       -- 标签脱落待补贴（教师侧警示）
    pending_class_id INTEGER REFERENCES classes(id), -- 转班时睡袋在家长家：送回后要落位的班级
    pending_slot_id  INTEGER REFERENCES cubby_slots(id),
    started_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    ended_at     TEXT
    -- 转班/换袋时结束旧行再开新行；活动期唯一性由下方部分唯一索引保证
);
-- 核心约束：同一个睡袋在任一时刻只有一条活动分配（不能同时分给两个孩子）
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_bag
    ON assignments(bag_id) WHERE ended_at IS NULL;
-- 一个孩子在任一时刻只有一条活动分配（一个孩子一个在用睡袋）
CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_child
    ON assignments(child_id) WHERE ended_at IS NULL;
-- 格位互斥：同一格位在任一时刻只能属于一个活动分配（人在领回期间格位仍为其保留）
CREATE UNIQUE INDEX IF NOT EXISTS idx_slot_exclusive
    ON assignments(slot_id) WHERE ended_at IS NULL AND slot_id IS NOT NULL;

-- 物品链：每次状态变化都落一条不可变事件
CREATE TABLE IF NOT EXISTS bag_events (
    id           INTEGER PRIMARY KEY,
    bag_id       INTEGER NOT NULL REFERENCES sleeping_bags(id),
    child_id     INTEGER REFERENCES children(id),
    event_type   TEXT NOT NULL CHECK(event_type IN (
                     'registered',          -- 登记
                     'assigned',            -- 分配给孩子
                     'taken_home',          -- 周末领回
                     'returned',            -- 周一送回（必须见实物核验）
                     'missing_label',       -- 标签脱落处理
                     'wrong_swap',          -- 睡袋拿错
                     'not_returned',        -- 周一未带回
                     'damaged_replace',     -- 污损更换
                     'transfer_handover',   -- 转班交接
                     'spare_issued',        -- 备用袋启用
                     'spare_returned'       -- 备用袋归还入库
                 )),
    actor        TEXT NOT NULL DEFAULT '',          -- 操作人（老师/家长）
    auth_id      INTEGER REFERENCES pickup_auths(id), -- 代领时的授权
    clean_status TEXT NOT NULL DEFAULT '',          -- 核验的清洁状态
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 代领授权：有有效期；撤销后旧码立即失效
CREATE TABLE IF NOT EXISTS pickup_auths (
    id           INTEGER PRIMARY KEY,
    child_id     INTEGER NOT NULL REFERENCES children(id),
    bag_id       INTEGER NOT NULL REFERENCES sleeping_bags(id),
    code         TEXT NOT NULL UNIQUE,       -- 扫码用授权码
    proxy_name   TEXT NOT NULL,              -- 代领人
    proxy_phone  TEXT NOT NULL DEFAULT '',
    granted_by   INTEGER REFERENCES parents(id),
    valid_from   TEXT NOT NULL,
    valid_until  TEXT NOT NULL,
    revoked_at   TEXT,                        -- 非空=已撤销，立即失效
    used_at      TEXT,                        -- 已使用则不可重复领取
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 备用袋库存流水
CREATE TABLE IF NOT EXISTS spare_bags (
    id           INTEGER PRIMARY KEY,
    bag_id       INTEGER NOT NULL UNIQUE REFERENCES sleeping_bags(id),
    in_stock     INTEGER NOT NULL DEFAULT 1,  -- 1=在库可借
    note         TEXT NOT NULL DEFAULT ''
);

-- 转班交接单
CREATE TABLE IF NOT EXISTS transfers (
    id            INTEGER PRIMARY KEY,
    child_id      INTEGER NOT NULL REFERENCES children(id),
    bag_id        INTEGER NOT NULL REFERENCES sleeping_bags(id),
    from_class    INTEGER NOT NULL REFERENCES classes(id),
    to_class      INTEGER NOT NULL REFERENCES classes(id),
    new_slot_id   INTEGER REFERENCES cubby_slots(id),
    status        TEXT NOT NULL DEFAULT 'completed'
                  CHECK(status IN ('completed','bag_away_pending')), -- 睡袋在家长家时先挂起
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 本地标签照片（文件存 data/photos/，库里只存路径与元信息）
CREATE TABLE IF NOT EXISTS label_photos (
    id           INTEGER PRIMARY KEY,
    bag_id       INTEGER NOT NULL REFERENCES sleeping_bags(id),
    rel_path     TEXT NOT NULL,
    content_type TEXT NOT NULL DEFAULT 'image/jpeg',
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 模拟家长消息（适配器返回，不真正外发）
CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY,
    parent_phone TEXT NOT NULL,
    channel      TEXT NOT NULL DEFAULT 'mock',
    kind         TEXT NOT NULL,              -- pickup_reminder/return_reminder/...
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- 进程内提醒任务的落库记录（重启后可恢复待办）
CREATE TABLE IF NOT EXISTS reminders (
    id           INTEGER PRIMARY KEY,
    fire_at      TEXT NOT NULL,              -- 计划触发时间
    kind         TEXT NOT NULL,
    ref_type     TEXT NOT NULL DEFAULT '',
    ref_id       INTEGER,
    payload      TEXT NOT NULL DEFAULT '{}',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK(status IN ('pending','done','cancelled')),
    done_at      TEXT,
    created_at   TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
