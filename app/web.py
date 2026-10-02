"""Web 界面（标准库 http.server，零第三方依赖）。

- 教师端：/class/<id> 以儿童代号 + 格位号定位；领回/送回核验、代领授权、
  转班、备用袋、错拿登记、标签照片、消息回执。
- 家长端：/parent?phone=... 仅显示本人孩子的睡袋状态与本人授权。
- 领回与送回使用不同图标与文字；温和底色 + 高对比操作区，无卡通装饰。
"""
from __future__ import annotations

import html
import json
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import services as svc
from .db import connect, init_db
from .messages import list_messages
from .photos import photo_path, save_label_photo
from .reminders import (ReminderScheduler, schedule_monday_returns,
                        schedule_weekend_pickups)

DB_PATH = os.environ.get("NAP_DB")


def get_conn():
    return connect(DB_PATH)


# ---------------------------------------------------------------- 小工具

def h(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def qs(url: str, **params) -> str:
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    return f"{url}?{q}" if q else url


def parse_form(body: bytes, content_type: str) -> dict[str, str]:
    ctype = content_type or ""
    if "multipart/form-data" in ctype:
        return _parse_multipart(body, ctype)[0]
    parsed = urllib.parse.parse_qs(body.decode("utf-8"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


def parse_multipart_files(body: bytes, content_type: str):
    return _parse_multipart(body, content_type)


def _parse_multipart(body: bytes, content_type: str):
    boundary = None
    for part in content_type.split(";"):
        part = part.strip()
        if part.startswith("boundary="):
            boundary = part[len("boundary="):].strip('"')
    if not boundary:
        return {}, {}
    delim = b"--" + boundary.encode()
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    for chunk in body.split(delim):
        if not chunk or chunk in (b"--", b"--\r\n", b"\r\n"):
            continue
        chunk = chunk.strip(b"\r\n")
        if b"\r\n\r\n" not in chunk:
            continue
        head, content = chunk.split(b"\r\n\r\n", 1)
        headers = head.decode("utf-8", "replace")
        name = filename = ctype2 = ""
        for line in headers.split("\r\n"):
            low = line.lower()
            if low.startswith("content-disposition:"):
                for seg in low.split(";"):
                    seg = seg.strip()
                    if seg.startswith("name="):
                        name = seg[5:].strip('"')
                    if seg.startswith("filename="):
                        filename = seg[9:].strip('"')
            elif low.startswith("content-type:"):
                ctype2 = line.split(":", 1)[1].strip()
        if not name:
            continue
        if filename:
            files[name] = (filename, content, ctype2 or "application/octet-stream")
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


STATUS_STYLE = {
    "at_school": ("在园", "chip-in"),
    "taken_home": ("已领回", "chip-out"),
    "spare_in_use": ("备用袋使用中", "chip-spare"),
}

# 图标：领回（向下出园）与送回（核对后回园）刻意做成不同形状 + 不同文字
ICON_PICKUP = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path '
    'd="M12 3v12m0 0l-4.5-4.5M12 15l4.5-4.5M4 19h16" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"/></svg>')
ICON_RETURN = (
    '<svg viewBox="0 0 24 24" aria-hidden="true"><path '
    'd="M9 4L4 9l5 5M4 9h11a5 5 0 010 10h-3" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round"/><path d="M16.5 16.5L19 19l2.5-2.5" fill="none" '
    'stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>')


# ---------------------------------------------------------------- 页面骨架

CSS = """
:root{
  --bg:#faf7f2; --panel:#ffffff; --ink:#21403c; --ink-soft:#556964;
  --line:#e3ddd2; --primary:#0f6e64; --primary-dark:#0b5149;
  --primary-soft:#e3f0ee; --amber:#8a4b08; --amber-soft:#fdf0dd;
  --danger:#a32a1e; --danger-soft:#fbe7e3;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
a{color:var(--primary-dark)}
.wrap{max-width:1080px;margin:0 auto;padding:20px}
.topbar{background:var(--primary-dark);color:#fff;padding:12px 20px;
  display:flex;gap:18px;align-items:center;flex-wrap:wrap}
.topbar a{color:#eafaf7;text-decoration:none;font-weight:600}
.topbar .brand{font-weight:700;font-size:16px}
h1{font-size:20px;margin:0 0 4px} h2{font-size:16px;margin:22px 0 8px}
.sub{color:var(--ink-soft);margin:0 0 14px;font-size:13.5px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px;
  padding:16px;margin-bottom:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);
  vertical-align:top}
th{color:var(--ink-soft);font-weight:600;background:#f4f0e8}
.chip{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12.5px;
  font-weight:700;border:1px solid transparent;white-space:nowrap}
.chip-in{background:var(--primary-soft);color:var(--primary-dark);border-color:#bcd8d3}
.chip-out{background:#eef0f3;color:#3b4a55;border-color:#d3d9e0}
.chip-spare{background:var(--amber-soft);color:var(--amber);border-color:#ecd3b3}
.warn{background:var(--danger-soft);color:var(--danger);border:1px solid #ecc7bf;
  border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13.5px;font-weight:600}
.note{background:var(--amber-soft);color:var(--amber);border:1px solid #ecd3b3;
  border-radius:8px;padding:8px 12px;margin:8px 0;font-size:13.5px}
.ok{background:var(--primary-soft);color:var(--primary-dark);
  border:1px solid #bcd8d3;border-radius:8px;padding:8px 12px;margin:8px 0;
  font-size:13.5px;font-weight:600}
label{display:block;font-size:13px;color:var(--ink-soft);margin:8px 0 3px;font-weight:600}
input,select,textarea{width:100%;padding:8px 10px;border:1px solid #c9c2b4;
  border-radius:8px;font:inherit;background:#fff;color:var(--ink)}
input[type=checkbox]{width:auto;margin-right:6px}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end}
.row > div{flex:1;min-width:120px}
.btn{display:inline-flex;align-items:center;gap:7px;border:0;border-radius:9px;
  padding:9px 14px;font:inherit;font-weight:700;cursor-pointer;text-decoration:none}
.btn svg{width:18px;height:18px}
.btn-pickup{background:#1f6feb;color:#fff}
.btn-pickup:hover{background:#155bca}
.btn-return{background:var(--primary);color:#fff}
.btn-return:hover{background:var(--primary-dark)}
.btn-ghost{background:#fff;color:var(--ink);border:1px solid #c9c2b4}
.btn-ghost:hover{background:#f1ece2}
.btn-danger{background:#fff;color:var(--danger);border:1px solid #d9a89f}
.btn-danger:hover{background:var(--danger-soft)}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.small{font-size:12.5px;color:var(--ink-soft)}
.code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
  background:#f4f0e8;padding:1px 6px;border-radius:5px;font-size:13px}
.card{border:1px solid var(--line);border-radius:12px;padding:14px;background:#fff}
.card .hd{display:flex;justify-content:space-between;gap:8px;align-items:center}
.bigcode{font-size:17px;font-weight:800}
.meta{color:var(--ink-soft);font-size:13px;margin:2px 0}
.tag{display:inline-block;background:#f4f0e8;border:1px solid var(--line);
  border-radius:6px;padding:0 6px;font-size:12px;margin-right:4px}
.timeline{border-left:3px solid #d7e4e1;margin-left:6px;padding-left:14px}
.timeline .ev{margin:0 0 10px}
.timeline .t{font-size:12px;color:var(--ink-soft)}
.muted-empty{color:var(--ink-soft);font-style:italic}
.navtabs{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px}
.navtabs a{padding:6px 12px;border-radius:999px;background:#fff;border:1px solid var(--line);
 text-decoration:none;font-size:13.5px;font-weight:600}
.navtabs a.on{background:var(--primary);color:#fff;border-color:var(--primary)}
"""


def page(title: str, body: str) -> bytes:
    doc = f"""<!doctype html><html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{h(title)}</title><style>{CSS}</style></head><body>
<nav class="topbar">
  <span class="brand">午睡袋管理</span>
  <a href="/">班级总览</a>
  <a href="/spares">备用袋库存</a>
  <a href="/messages">消息回执</a>
  <a href="/parent">家长端</a>
</nav><main class="wrap">{body}</main></body></html>"""
    return doc.encode("utf-8")


def flash(req) -> str:
    out = []
    msg = req.params.get("msg")
    if msg:
        out.append(f'<div class="ok">✓ {h(msg)}</div>')
    err = req.params.get("err")
    if err:
        out.append(f'<div class="warn">✕ {h(err)}</div>')
    return "".join(out)


# ---------------------------------------------------------------- 页面渲染

def render_home(conn) -> bytes:
    classes = svc.list_classes(conn)
    cards = "".join(
        f'<a class="card" style="text-decoration:none;color:inherit;display:block" '
        f'href="/class/{c["id"]}"><div class="hd"><span class="bigcode">{h(c["name"])}</span>'
        f'<span class="chip chip-in">进入班级页</span></div>'
        f'<p class="meta">按儿童代号与格位号定位睡袋</p></a>'
        for c in classes)
    body = f"""
    <h1>班级总览</h1>
    <p class="sub">教师端。登记与核验均需面对实物操作；所有状态变化写入物品链可追溯。</p>
    <div class="grid">{cards}</div>
    <h2>说明</h2>
    <div class="panel small">
    <p>领回操作使用 <span class="chip chip-out">⬇ 图标 + “扫码领回”</span>；
    周一送回使用 <span class="chip chip-in">↩ 图标 + “核对实物后送回”</span>，
    两者图标、文字、颜色均不同。</p>
    <p>家长端入口在顶部“家长端”，凭手机号进入，仅显示本人孩子的睡袋状态。</p>
    </div>"""
    return page("午睡袋管理 · 班级总览", body)


def render_class(conn, class_id: int, req) -> bytes:
    rows = svc.class_board(conn, class_id)
    if not rows:
        return page("班级", '<div class="warn">班级不存在或没有孩子</div>'
                           '<p><a href="/">返回总览</a></p>')
    class_name = rows[0]["class_name"]
    other_classes = [c for c in svc.list_classes(conn) if c["id"] != class_id]
    spares = svc.spare_stock(conn)
    spare_nos = "".join(f'<option value="{h(s["bag_no"])}">{h(s["bag_no"])}'
                        f'（{"在库" if s["in_stock"] else "借出"}）</option>'
                        for s in spares)

    cards = []
    for r in rows:
        status_text, style = STATUS_STYLE.get(r["status"] or "", ("未分配", "chip-out"))
        if not r["bag_no"]:
            status_text, style = "未分配", "chip-out"
        alerts = []
        if r["label_damaged"]:
            alerts.append('<div class="warn">标签脱落：待补贴新标签</div>')
        if r["status"] == "taken_home":
            alerts.append('<div class="note">睡袋在家长家中 · 等待周一送回核验</div>')
        if r["pending_class_id"]:
            pc = conn.execute("SELECT name FROM classes WHERE id=?",
                              (r["pending_class_id"],)).fetchone()
            ps = conn.execute("SELECT slot_no FROM cubby_slots WHERE id=?",
                              (r["pending_slot_id"],)).fetchone()
            alerts.append(f'<div class="note">转班挂起：送回核验后自动落位 '
                          f'{h(pc["name"] if pc else "?")} 班 '
                          f'{h(ps["slot_no"] if ps else "?")} 格</div>')
        bag_block = (f'<p class="meta">睡袋 <span class="code">{h(r["bag_no"])}</span>'
                     f' · 材质 {h(r["material"] or "未填")}</p>'
                     f'<p class="meta">过敏注意：{h(r["bag_allergy"] or "无")}</p>') \
            if r["bag_no"] else '<p class="meta muted-empty">尚无登记睡袋</p>'
        # 领回：扫码（家长本人码/代领码），与送回完全不同的按钮
        pickup_form = f"""
        <form method="post" action="/action/pickup">
          <input type="hidden" name="class_id" value="{class_id}">
          <label>出示领回码（家长本人码或代领码）</label>
          <div class="row"><div><input name="code" required placeholder="如 P-AB12CD / D-3F…"></div>
          <div><button class="btn btn-pickup">{ICON_PICKUP}扫码领回</button></div></div>
        </form>""" if r["status"] != "taken_home" else ""
        # 送回核验：必须勾选“已见实物”
        return_form = ""
        if r["status"] == "taken_home":
            return_form = f"""
        <form method="post" action="/action/return">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="child_code" value="{h(r["child_code"])}">
          <label>核对实物标签编号</label>
          <input name="bag_no" required value="{h(r["bag_no"])}" class="code">
          <div class="row">
            <div><label>清洁状态</label>
              <select name="clean_status">
                <option value="clean">清洁合格</option>
                <option value="needs_wash">需补洗</option>
                <option value="dirty">污损不合格</option>
              </select></div>
            <div><label>标签</label>
              <select name="label_intact">
                <option value="1">标签完好</option>
                <option value="0">标签脱落</option>
              </select></div>
          </div>
          <label style="margin-top:8px"><input type="checkbox" name="seen" value="1">
          我已当面见到实物并核对（不勾选无法送回）</label>
          <div class="actions"><button class="btn btn-return">{ICON_RETURN}核对实物后送回</button></div>
        </form>
        <form method="post" action="/action/not-returned"
              onsubmit="return confirm('登记为周一未带回？')">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="child_code" value="{h(r["child_code"])}">
          <button class="btn btn-ghost">登记周一未带回</button>
        </form>"""
        # 教师操作：代领授权/撤销/转班/污损更换/照片
        parent = conn.execute("SELECT * FROM parents WHERE child_id=?",
                              (r["child_id"],)).fetchone()
        auths = list(conn.execute(
            """SELECT pa.* FROM pickup_auths pa WHERE pa.child_id=?
               ORDER BY pa.id DESC""", (r["child_id"],)))
        auth_rows = "".join(
            f'<tr><td class="code">{h(a["code"])}</td><td>{h(a["proxy_name"])}</td>'
            f'<td class="small">{h(a["valid_until"])}</td>'
            f'<td>{_auth_state(a)}</td>'
            f'<td><form method="post" action="/action/revoke" style="display:inline">'
            f'<input type="hidden" name="class_id" value="{class_id}">'
            f'<input type="hidden" name="code" value="{h(a["code"])}">'
            f'<button class="btn btn-danger" style="padding:4px 10px;font-size:12px">'
            f'撤销（旧码立即失效）</button></form></td></tr>'
            for a in auths) or '<tr><td colspan="5" class="muted-empty">暂无代领授权</td></tr>'
        parent_phone = parent["phone"] if parent else ""
        admin = ""
        if r["bag_no"]:
            to_opts = "".join(f'<option value="{c["id"]}">{h(c["name"])}</option>'
                              for c in other_classes)
            admin = f"""
        <details style="margin-top:8px"><summary class="small" style="cursor:pointer;
          font-weight:600">教师操作：代领授权 / 转班 / 污损更换 / 标签照片</summary>
        <div class="row" style="margin-top:8px">
          <form method="post" action="/action/grant" style="flex:2;min-width:240px">
            <input type="hidden" name="class_id" value="{class_id}">
            <input type="hidden" name="parent_phone" value="{h(parent_phone)}">
            <label>新增代领授权（家长 {h(parent["name"] if parent else "?")} 授权）</label>
            <div class="row">
              <div><input name="proxy_name" required placeholder="代领人姓名"></div>
              <div><input name="proxy_phone" placeholder="代领人手机号"></div>
              <div><label>有效天数</label><input name="days" type="number" value="2" min="1"></div>
              <div style="flex:0"><button class="btn btn-ghost">生成授权码</button></div>
            </div>
          </form>
        </div>
        <table style="margin-top:8px"><tr><th>授权码</th><th>代领人</th><th>有效期至</th>
        <th>状态</th><th></th></tr>{auth_rows}</table>
        <form method="post" action="/action/transfer" style="margin-top:10px">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="child_code" value="{h(r["child_code"])}">
          <label>转班交接（睡袋在家长家时自动挂起，送回时落位）</label>
          <div class="row"><div><select name="to_class_id">{to_opts}</select></div>
          <div><input name="new_slot_no" required placeholder="新班格位号，如 B-03"></div>
          <div style="flex:0"><button class="btn btn-ghost">转班交接</button></div></div>
        </form>
        <form method="post" action="/action/spare-issue" style="margin-top:10px">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="child_code" value="{h(r["child_code"])}">
          <input type="hidden" name="damaged_bag_no" value="{h(r["bag_no"])}">
          <label>污损更换：原袋报废并启用备用袋</label>
          <div class="row"><div><select name="spare_bag_no">{spare_nos}</select></div>
          <div><input name="reason" required placeholder="污损原因"></div>
          <div style="flex:0"><button class="btn btn-danger">报废并启用备用袋</button></div></div>
        </form>
        <form method="post" action="/action/photo" enctype="multipart/form-data"
              style="margin-top:10px">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="bag_no" value="{h(r["bag_no"])}">
          <label>上传标签照片（本地保存）</label>
          <div class="row"><div><input type="file" name="file" accept="image/*" required></div>
          <div style="flex:0"><button class="btn btn-ghost">保存照片</button></div></div>
        </form>
        <p class="small" style="margin-top:6px">
          物品链：<a href="/bag/{urllib.parse.quote(r["bag_no"])}">查看 {h(r["bag_no"])} 全部事件</a></p>
        </details>"""
        allergy = h(r["child_allergy"] or "无过敏备注")
        cards.append(f"""
        <section class="card">
          <div class="hd">
            <div><span class="bigcode">{h(r["child_code"])}</span>
              <span class="tag">格位 {h(r["slot_no"] or "未上格")}</span></div>
            <span class="chip {style}">{status_text}</span>
          </div>
          <p class="meta">过敏注意：{allergy}</p>
          {bag_block}{"".join(alerts)}
          {pickup_form}{return_form}{admin}
        </section>""")

    # 错拿登记 + 排提醒（班级级）
    child_opts = "".join(f'<option value="{h(r["child_code"])}">{h(r["child_code"])}</option>'
                         for r in rows)
    body = f"""
    <div class="navtabs">
      {''.join(f'<a href="/class/{c["id"]}" class="on">{h(c["name"])}</a>'
                if c["id"] == class_id else
                f'<a href="/class/{c["id"]}">{h(c["name"])}</a>'
                for c in svc.list_classes(conn))}
    </div>
    <h1>{h(class_name)}</h1>
    <p class="sub">以儿童代号与格位号定位，不使用儿童照片或卡通装饰。</p>
    {flash(req)}
    <div class="grid">{"".join(cards)}</div>
    <h2>错拿与提醒（全班）</h2>
    <div class="panel">
      <form method="post" action="/action/swap">
        <input type="hidden" name="class_id" value="{class_id}">
        <label>两家领回后发现实物互错，登记错拿（双方家长发提醒，周一先换回再核验）</label>
        <div class="row"><div><select name="code_a">{child_opts}</select></div>
        <div><select name="code_b">{child_opts}</select></div>
        <div style="flex:0"><button class="btn btn-danger">登记错拿</button></div></div>
      </form>
      <div class="row" style="margin-top:12px">
        <form method="post" action="/action/reminders">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="kind" value="weekend">
          <button class="btn btn-ghost">排入周末领回提醒</button>
        </form>
        <form method="post" action="/action/reminders">
          <input type="hidden" name="class_id" value="{class_id}">
          <input type="hidden" name="kind" value="monday">
          <button class="btn btn-ghost">排入周一送回提醒</button>
        </form>
      </div>
    </div>"""
    return page(f"{class_name} · 班级页", body)


def _auth_state(a) -> str:
    if a["revoked_at"]:
        return '<span class="chip chip-out">已撤销·码失效</span>'
    if a["used_at"]:
        return '<span class="chip chip-in">已使用</span>'
    return '<span class="chip chip-spare">有效</span>'


def render_bag(conn, bag_no: str, req) -> bytes:
    chain = svc.bag_chain(conn, bag_no)
    bag = chain["bag"]
    evs = "".join(
        f'<div class="ev"><b>{h(_event_text(e["event_type"]))}</b> '
        f'<span class="small">操作人 {h(e["actor"] or "-")}'
        f'{(" · 清洁：" + h(svc.CLEAN_LABELS.get(e["clean_status"], e["clean_status"]))) if e["clean_status"] else ""}'
        f'</span><div class="t">{h(e["created_at"])}'
        f'{(" · " + h(e["note"])) if e["note"] else ""}</div></div>'
        for e in chain["events"])
    photos = "".join(
        f'<a href="/photos/{p["id"]}" target="_blank">'
        f'<img src="/photos/{p["id"]}" width="96" height="96" '
        f'style="object-fit:cover;border-radius:8px;border:1px solid var(--line)" '
        f'alt="标签照片"></a>'
        for p in chain["photos"]) or '<span class="muted-empty">暂无标签照片</span>'
    body = f"""
    <p><a href="/">← 班级总览</a></p>
    <h1>睡袋物品链 · <span class="code">{h(bag["bag_no"])}</span></h1>
    {flash(req)}
    <div class="panel">
      <p class="meta">材质：{h(bag["material"] or "未填")} ·
      过敏注意：{h(bag["allergy_note"] or "无")} ·
      类型：{"备用袋" if bag["spare"] else "常规袋"} ·
      状态：{"已报废：" + h(bag["retire_reason"]) if bag["retired"] else "流通中"}</p>
    </div>
    <h2>标签照片（本地保存）</h2>
    <div class="panel" style="display:flex;gap:10px;flex-wrap:wrap">{photos}</div>
    <h2>事件链（不可变）</h2>
    <div class="panel timeline">{evs}</div>"""
    return page(f"睡袋 {bag['bag_no']} · 物品链", body)


def _event_text(k: str) -> str:
    return {"registered": "登记", "assigned": "分配", "taken_home": "周末领回",
            "returned": "周一送回", "missing_label": "标签脱落",
            "wrong_swap": "睡袋拿错", "not_returned": "周一未带回",
            "damaged_replace": "污损更换", "transfer_handover": "转班交接",
            "spare_issued": "备用袋启用", "spare_returned": "备用袋归还"}.get(k, k)


def render_spares(conn, req) -> bytes:
    rows = svc.spare_stock(conn)
    trs = "".join(
        f'<tr><td class="code">{h(r["bag_no"])}</td><td>{h(r["material"] or "-")}</td>'
        f'<td><span class="chip {"chip-in" if r["in_stock"] else "chip-spare"}">'
        f'{"在库" if r["in_stock"] else "借出中（" + h(r["used_by_code"] or "?") + "）"}'
        f'</span></td><td class="small">{h(r["note"] or "")}</td></tr>'
        for r in rows) or '<tr><td colspan="4" class="muted-empty">备用袋库存为空</td></tr>'
    body = f"""<h1>备用袋库存</h1>{flash(req)}
    <div class="panel"><table><tr><th>编号</th><th>材质</th><th>库存状态</th><th>备注</th></tr>
    {trs}</table></div>"""
    return page("备用袋库存", body)


def render_messages(conn, req) -> bytes:
    rows = list_messages(conn, limit=100)
    trs = "".join(
        f'<tr><td>{h(r["created_at"])}</td><td class="code">{h(r["parent_phone"])}</td>'
        f'<td><b>{h(r["title"])}</b><div class="small">{h(r["body"])}</div></td>'
        f'<td class="small">{h(r["kind"])} · 模拟适配器</td></tr>'
        for r in rows) or '<tr><td colspan="4" class="muted-empty">暂无消息（适配器不外发，仅返回回执）</td></tr>'
    body = f"""<h1>家长消息 · 模拟适配器回执</h1>{flash(req)}
    <div class="panel"><table><tr><th>时间</th><th>接收家长</th><th>内容</th><th>类型</th></tr>
    {trs}</table></div>"""
    return page("消息回执", body)


def render_parent_login(req) -> bytes:
    body = f"""
    <h1>家长端</h1>
    <p class="sub">凭预留手机号进入，仅显示您本人孩子的睡袋状态与代领授权。</p>
    {flash(req)}
    <form class="panel" method="get" action="/parent" style="max-width:420px">
      <label>预留手机号</label>
      <input name="phone" required placeholder="如 13800000001" inputmode="numeric">
      <div class="actions"><button class="btn btn-return" type="submit">进入</button></div>
    </form>"""
    return page("家长端登录", body)


def render_parent(conn, phone: str, req) -> bytes:
    data = svc.parent_dashboard(conn, phone)
    p, v = data["parent"], data["view"]
    if not v or not v["bag_no"]:
        status = '<span class="chip chip-out">尚无登记睡袋</span>'
        bag = '<p class="meta muted-empty">老师尚未登记睡袋。</p>'
        pickup_btn = ""
    else:
        text, style = STATUS_STYLE.get(v["status"], ("未知", "chip-out"))
        status = f'<span class="chip {style}">{text}</span>'
        bag = (f'<p class="meta">睡袋编号 <span class="code">{h(v["bag_no"])}</span>'
               f' · 存放格位 <b>{h(v["slot_no"] or "未上格")}</b></p>'
               f'<p class="meta">材质 {h(v["material"] or "未填")} · '
               f'过敏注意 {h(v["bag_allergy"] or "无")}</p>')
        if v["status"] != "taken_home":
            pickup_btn = f"""
            <form method="post" action="/action/pickup">
              <input type="hidden" name="code" value="{h(p["pickup_code"])}">
              <input type="hidden" name="back" value="parent">
              <input type="hidden" name="phone" value="{h(phone)}">
              <button class="btn btn-pickup">{ICON_PICKUP}扫码领回（我的领回码 {h(p["pickup_code"])}）</button>
            </form>"""
        else:
            pickup_btn = (f'<div class="note">睡袋已领回在家。请清洗后于周一送回，'
                          f'由老师 <b>{ICON_RETURN} 核对实物后送回</b> 核验。</div>')
    auths = data["auths"]
    auth_rows_list = []
    for a in auths:
        if a["revoked_at"]:
            revoke_cell = ""
        else:
            revoke_cell = (
                '<form method="post" action="/action/revoke">'
                f'<input type="hidden" name="back" value="parent">'
                f'<input type="hidden" name="phone" value="{h(phone)}">'
                f'<input type="hidden" name="code" value="{h(a["code"])}">'
                f'<input type="hidden" name="parent_phone" value="{h(phone)}">'
                '<button class="btn btn-danger" style="padding:4px 10px;font-size:12px">'
                '撤销（旧码立即失效）</button></form>')
        auth_rows_list.append(
            f'<tr><td class="code">{h(a["code"])}</td><td>{h(a["proxy_name"])}</td>'
            f'<td class="small">{h(a["valid_from"])} 至 {h(a["valid_until"])}</td>'
            f'<td>{_auth_state(a)}</td><td>{revoke_cell}</td></tr>')
    trs = "".join(auth_rows_list) \
        or '<tr><td colspan="5" class="muted-empty">暂无代领授权</td></tr>'
    msgs = list_messages(conn, phone=phone, limit=10)
    msg_html = "".join(
        f'<div class="ev"><b>{h(m["title"])}</b><div class="t">{h(m["created_at"])}</div>'
        f'<div class="small">{h(m["body"])}</div></div>' for m in msgs) \
        or '<p class="muted-empty">暂无消息</p>'
    body = f"""
    <h1>{h(v["child_code"] if v else "")} 的睡袋</h1>
    <p class="sub">家长：{h(p["name"])} · {h(v["class_name"] if v else "")} ·
    儿童代号 {h(v["child_code"] if v else "")}（仅您本人可见）</p>
    {flash(req)}
    <div class="panel"><div class="hd"><h2 style="margin:0">当前状态</h2>{status}</div>
      {bag}
      {pickup_btn}
      {'<p class="small" style="margin-top:8px">⚠ 标签如有脱落请先告知老师补贴，再做送回核验。</p>' if v and v["label_damaged"] else ""}
    </div>
    <h2>代领授权</h2>
    <div class="panel">
      <form method="post" action="/action/grant">
        <input type="hidden" name="back" value="parent">
        <input type="hidden" name="phone" value="{h(phone)}">
        <input type="hidden" name="parent_phone" value="{h(phone)}">
        <label>授权他人代领（有有效期，可随时撤销）</label>
        <div class="row">
          <div><input name="proxy_name" required placeholder="代领人姓名"></div>
          <div><input name="proxy_phone" placeholder="代领人手机号"></div>
          <div><label>有效天数</label><input name="days" type="number" value="2" min="1"></div>
          <div style="flex:0"><button class="btn btn-ghost">生成授权码</button></div>
        </div>
      </form>
      <table style="margin-top:10px"><tr><th>授权码</th><th>代领人</th><th>有效期</th>
      <th>状态</th><th></th></tr>{trs}</table>
    </div>
    <h2>收到的消息</h2>
    <div class="panel timeline">{msg_html}</div>
    <p class="small"><a href="/parent">切换手机号</a></p>"""
    return page("家长端 · 我的睡袋", body)


# ---------------------------------------------------------------- Handler

class Handler(BaseHTTPRequestHandler):
    params: dict[str, str]

    def log_message(self, fmt, *args):
        pass  # 静默；需要时可打开

    def _send(self, content: bytes, status=200, ctype="text/html; charset=utf-8",
              extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(content)))
        for k, v in (extra_headers or []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(content)

    def _redirect(self, url: str):
        self._send(b"", HTTPStatus.SEE_OTHER, extra_headers=[("Location", url)])

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        self.params = {k: v[0] for k, v in
                       urllib.parse.parse_qs(parsed.query).items()}
        path = parsed.path
        conn = get_conn()
        try:
            if path == "/":
                self._send(render_home(conn))
            elif path.startswith("/class/"):
                cid = int(path.rsplit("/", 1)[-1])
                self._send(render_class(conn, cid, self))
            elif path.startswith("/bag/"):
                bag_no = urllib.parse.unquote(path.rsplit("/", 1)[-1])
                self._send(render_bag(conn, bag_no, self))
            elif path == "/spares":
                self._send(render_spares(conn, self))
            elif path == "/messages":
                self._send(render_messages(conn, self))
            elif path == "/parent":
                phone = self.params.get("phone")
                if not phone:
                    self._send(render_parent_login(self))
                else:
                    try:
                        self._send(render_parent(conn, phone, self))
                    except svc.NotFound:
                        self._send(render_parent_login(self))
            elif path.startswith("/photos/"):
                pid = int(path.rsplit("/", 1)[-1])
                try:
                    p, ctype = photo_path(pid, conn)
                    self._send(p.read_bytes(), ctype=ctype)
                except svc.NotFound:
                    self._send(b"not found", 404, "text/plain")
            else:
                self._send("<p>404</p><a href='/'>回家</a>".encode(), 404)
        except ValueError:
            self._send("参数错误".encode(), 400, "text/plain; charset=utf-8")
        finally:
            conn.close()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype:
            form, files = parse_multipart_files(body, ctype)
        else:
            form, files = parse_form(body, ctype), {}
        self.params = form
        path = urllib.parse.urlparse(self.path).path
        conn = get_conn()
        try:
            self._dispatch(conn, path, form, files)
        except (svc.DomainError, ValueError) as e:
            back = form.get("back")
            if back == "parent":
                target = qs("/parent", phone=form.get("phone"), err=str(e))
            else:
                target = qs(f"/class/{form.get('class_id')}", err=str(e)) \
                    if form.get("class_id") else qs("/", err=str(e))
            self._redirect(target)
        finally:
            conn.close()

    def _back(self, form, msg=None, err=None):
        if form.get("back") == "parent":
            return qs("/parent", phone=form.get("phone"), msg=msg, err=err)
        cid = form.get("class_id")
        if path_back := form.get("return_path"):
            return qs(path_back, msg=msg, err=err)
        return qs(f"/class/{cid}" if cid else "/", msg=msg, err=err)

    def _dispatch(self, conn, path, form, files):
        from datetime import datetime, timedelta

        if path == "/action/pickup":
            r = svc.pickup(conn, code=form["code"])
            self._redirect(self._back(form, msg=(
                f"已领回：{r['child_code']} 的睡袋 {r['bag_no']}（{r['actor']}）")))
        elif path == "/action/return":
            r = svc.verify_return(
                conn, bag_no=form["bag_no"], child_code=form["child_code"],
                clean_status=form["clean_status"],
                label_intact=form.get("label_intact") == "1",
                seen=form.get("seen") == "1")
            warn = "；".join(r["warnings"])
            self._redirect(self._back(
                form, msg=f"送回核验通过：{r['bag_no']} 已上格" + (f"（{warn}）" if warn else "")))
        elif path == "/action/not-returned":
            svc.mark_not_returned(conn, child_code=form["child_code"])
            self._redirect(self._back(form, msg="已登记周一未带回，并提醒家长"))
        elif path == "/action/grant":
            days = int(form.get("days") or 2)
            r = svc.grant_proxy(
                conn, parent_phone=form["parent_phone"],
                proxy_name=form["proxy_name"],
                proxy_phone=form.get("proxy_phone", ""),
                valid_until=datetime.now() + timedelta(days=days))
            self._redirect(self._back(form, msg=(
                f"代领授权已生成：{r['code']}，有效期至 {r['valid_until']}")))
        elif path == "/action/revoke":
            svc.revoke_proxy(conn, code=form["code"],
                             parent_phone=form.get("parent_phone") or None)
            self._redirect(self._back(form, msg="授权已撤销，旧码立即失效"))
        elif path == "/action/transfer":
            r = svc.transfer_child(
                conn, child_code=form["child_code"],
                to_class_id=int(form["to_class_id"]),
                new_slot_no=form["new_slot_no"])
            state = "当场交接完成" if r["status"] == "completed" else "睡袋在家长家，已挂起待送回落位"
            self._redirect(self._back(form, msg=f"转班交接：{state}"))
        elif path == "/action/swap":
            svc.record_swap_after_pickup(
                conn, code_a=form["code_a"], code_b=form["code_b"])
            self._redirect(self._back(form, msg="错拿已登记，双方家长均收到提醒"))
        elif path == "/action/spare-issue":
            svc.replace_with_spare(
                conn, child_code=form["child_code"],
                damaged_bag_no=form["damaged_bag_no"],
                spare_bag_no=form["spare_bag_no"], reason=form["reason"])
            self._redirect(self._back(form, msg="原袋已报废，备用袋已启用"))
        elif path == "/action/spare-return":
            svc.return_spare(conn, child_code=form["child_code"],
                             new_bag_no=form.get("new_bag_no") or None)
            self._redirect(self._back(form, msg="备用袋已归还入库"))
        elif path == "/action/reminders":
            if form.get("kind") == "weekend":
                n = schedule_weekend_pickups(conn)
            else:
                n = schedule_monday_returns(conn)
            self._redirect(self._back(form, msg=f"已排入 {n} 条提醒（进程内任务处理）"))
        elif path == "/action/photo":
            filename, data, ctype = files["file"]
            ext_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                       ".png": "image/png", ".webp": "image/webp"}
            lower = filename.lower()
            content_type = next((v for k, v in ext_map.items() if lower.endswith(k)),
                                ctype)
            save_label_photo(conn, bag_no=form["bag_no"], data=data,
                             content_type=content_type)
            self._redirect(self._back(form, msg="标签照片已本地保存"))
        else:
            self._send(b"unknown action", 404, "text/plain")


def serve(host: str = "127.0.0.1", port: int = 8000, db_path: str | None = None,
          start_reminders: bool = True):
    global DB_PATH
    if db_path:
        DB_PATH = db_path
    conn = get_conn()
    init_db(conn)
    conn.close()
    scheduler = None
    if start_reminders:
        scheduler = ReminderScheduler(get_conn, interval=10.0).start()
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"午睡袋管理已启动：http://{host}:{port}")
    if scheduler:
        print("进程内提醒任务已运行（每 10 秒轮询到期提醒）")
    return httpd


if __name__ == "__main__":
    serve().serve_forever()
