"""Web 界面：班级页 / 家长端 / 备用库存 / 物品链。

仅使用标准库 http.server。页面约定：
  * 班级页以「儿童代号 + 格位号」定位，不显示真实姓名；
  * 家长端凭令牌只看本人孩子的睡袋；
  * 领回（下载图标＋“领回清洗”）与送回核验（勾选盾牌图标＋“见实归位”）
    使用不同图标与文字；
  * 配色为温和米/青色调，正文与按钮保持 WCAG AA 级对比度；
  * 操作区只放功能性控件，不用卡通装饰。
"""
from __future__ import annotations

import html
import os
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .photos import render_label_png
from .service import NapService, ServiceError

STATUS_TEXT = {
    "stored": "在校格位",
    "taken_home": "已领回家",
    "return_pending": "待老师核验",
    "in_use": "使用中",
    "damaged": "已污损停用",
    "lost": "标签待查",
}

INCIDENT_TEXT = {
    "proxy_pickup": "家长代领",
    "label_off": "标签脱落",
    "mispick": "睡袋错拿",
    "monday_missing": "周一未带回",
    "damage_replace": "污损更换",
    "transfer": "转班交接",
}

# ---------- 图标：领回与送回刻意使用不同图形 ----------
ICON_PICKUP = (
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
    '<path fill="currentColor" d="M11 3h2v9.2l3.1-3.1 1.4 1.4L12 16l-5.5-5.5 1.4-1.4 3.1 3.1V3zm-7 14h2v2h12v-2h2v4H4v-4z"/>'
    "</svg>"
)
ICON_RETURN = (
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">'
    '<path fill="currentColor" d="M12 2l8 3v6c0 5-3.4 9.4-8 11-4.6-1.6-8-6-8-11V5l8-3zm-1.2 13.4l5-5-1.4-1.4-3.6 3.6-1.8-1.8L7.6 12l3.2 3.4z"/>'
    "</svg>"
)
ICON_ALERT = (
    '<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true">'
    '<path fill="currentColor" d="M13 14h-2V8h2v6zm0 3h-2v-2h2v2zM12 2L1 21h22L12 2z"/>'
    "</svg>"
)

CSS = """
:root{
  --bg:#faf6ef; --panel:#fffdf8; --ink:#1d3a38; --muted:#5b6f6c;
  --line:#e2d8c7; --teal:#2e6f62; --teal-dark:#23564b;
  --clay:#b45309; --clay-dark:#92440a; --rose:#a33a3a;
  --amber-bg:#fdf0dd; --rose-bg:#f8e7e4; --teal-bg:#e7f1ee;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font:15px/1.55 "PingFang SC","Microsoft YaHei",system-ui,sans-serif}
a{color:var(--teal-dark)}
header.top{background:var(--teal);color:#fff;padding:14px 22px;
  display:flex;align-items:center;gap:16px;flex-wrap:wrap}
header.top a{color:#fff;text-decoration:none;font-weight:600}
header.top .brand{font-size:17px;font-weight:700;letter-spacing:.5px}
main{max-width:1080px;margin:22px auto;padding:0 18px}
h1{font-size:21px;margin:0 0 14px}
h2{font-size:16px;margin:22px 0 10px;color:var(--teal-dark)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:16px 18px;margin-bottom:18px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(235px,1fr));gap:12px}
.slot{border:1px solid var(--line);border-radius:10px;background:var(--panel);
  padding:12px;display:flex;flex-direction:column;gap:6px;min-height:150px}
.slot.empty{background:transparent;border-style:dashed;color:var(--muted)}
.slot-head{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.slot-code{font-weight:700;color:var(--teal-dark);font-variant-numeric:tabular-nums}
.child-code{font-size:17px;font-weight:700}
.bag-no{font-variant-numeric:tabular-nums;color:var(--ink)}
.muted{color:var(--muted);font-size:13px}
.badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:99px;
  font-size:12.5px;font-weight:600;border:1px solid transparent}
.badge.stored{background:var(--teal-bg);color:var(--teal-dark);border-color:#bdd8d1}
.badge.taken_home{background:var(--amber-bg);color:var(--clay-dark);border-color:#ecd2ad}
.badge.return_pending{background:#fdf3d8;color:#7a5b00;border-color:#ecdca3}
.badge.damaged,.badge.lost{background:var(--rose-bg);color:var(--rose);border-color:#e4bdb6}
.badge.spare{background:#eef0f6;color:#3b4a66;border-color:#cfd6e6}
.actions{display:flex;flex-wrap:wrap;gap:7px;margin-top:auto;padding-top:8px}
button,.btn{font:inherit;font-weight:600;border-radius:8px;border:1px solid var(--line);
  padding:7px 11px;cursor:pointer;background:#fff;color:var(--ink);
  display:inline-flex;align-items:center;gap:6px;text-decoration:none}
button.primary,.btn.primary{background:var(--teal);border-color:var(--teal);color:#fff}
button.primary:hover{background:var(--teal-dark)}
button.pickup{background:var(--clay);border-color:var(--clay);color:#fff}
button.pickup:hover{background:var(--clay-dark)}
button.danger{color:var(--rose);border-color:#d9b3ad}
button.ghost{background:transparent}
.small{font-size:13px;padding:5px 9px}
form.inline{display:inline}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input[type=text],input[type=password],input[type=number],select,textarea{
  font:inherit;padding:7px 9px;border:1px solid var(--line);border-radius:8px;
  background:#fff;color:var(--ink);min-width:0}
label.field{display:flex;flex-direction:column;gap:3px;font-size:13px;color:var(--muted)}
.seen-check{display:flex;gap:8px;align-items:flex-start;background:var(--teal-bg);
  border:1px solid #bdd8d1;border-radius:8px;padding:9px 11px;margin:8px 0;font-weight:600}
table{border-collapse:collapse;width:100%;font-size:14px}
th,td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:13px}
.notice{background:var(--amber-bg);border:1px solid #ecd2ad;color:var(--clay-dark);
  border-radius:10px;padding:11px 14px;margin-bottom:16px;display:flex;gap:10px}
.notice.err{background:var(--rose-bg);border-color:#e4bdb6;color:var(--rose)}
.notice.ok{background:var(--teal-bg);border-color:#bdd8d1;color:var(--teal-dark)}
.timeline{list-style:none;margin:0;padding:0;border-left:3px solid #d9e5e1}
.timeline li{padding:6px 0 6px 14px;margin-left:8px;position:relative}
.timeline li::before{content:"";position:absolute;left:-7.5px;top:12px;width:12px;height:12px;
  border-radius:50%;background:var(--teal);border:2px solid var(--panel)}
.timeline .ts{color:var(--muted);font-size:12.5px;margin-right:8px}
.tag{font-size:12px;background:#eef0f6;border-radius:6px;padding:1px 7px;color:#3b4a66}
.chain-photo{max-width:320px;border:1px solid var(--line);border-radius:10px}
.stat{display:flex;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.stat .box{background:var(--panel);border:1px solid var(--line);border-radius:10px;
  padding:10px 16px;font-weight:600}
.stat .box b{font-size:20px;color:var(--teal-dark)}
footer{color:var(--muted);text-align:center;font-size:12.5px;margin:26px 0}
@media print{header.top,.actions{display:none}}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v), quote=True)


def badge(status: str) -> str:
    return (f'<span class="badge {esc(status)}">{esc(STATUS_TEXT.get(status, status))}</span>')


class NapWebApp:
    def __init__(self, service: NapService, photo_dir: str):
        self.svc = service
        self.photo_dir = photo_dir

    def make_handler(self):
        app = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # 安静一点
                pass

            def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8"):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _html(self, body: str, code: int = 200):
                self._send(code, body.encode("utf-8"))

            def _redirect(self, path: str):
                self.send_response(HTTPStatus.SEE_OTHER)
                self.send_header("Location", path)
                self.end_headers()

            def _form(self):
                length = int(self.headers.get("Content-Length", 0))
                raw = self.rfile.read(length).decode("utf-8") if length else ""
                return {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

            def _qs(self):
                q = urllib.parse.urlparse(self.path).query
                return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

            def do_GET(self):
                try:
                    path = urllib.parse.urlparse(self.path).path
                    if path in ("/", "/classes"):
                        self._html(app.page_classes(self._qs()))
                    elif path == "/class":
                        self._html(app.page_class(self._qs()))
                    elif path == "/guardian":
                        self._html(app.page_guardian(self._qs()))
                    elif path == "/spares":
                        self._html(app.page_spares(self._qs()))
                    elif path == "/incidents":
                        self._html(app.page_incidents(self._qs()))
                    elif path == "/bag":
                        self._html(app.page_bag(self._qs()))
                    elif path == "/label_photo":
                        app.serve_photo(self, self._qs())
                    else:
                        self._html(app.layout("页面不存在", "<p>404</p>"), 404)
                except ServiceError as e:
                    self._html(app.layout("操作无法完成",
                                          f'<div class="notice err">{ICON_ALERT}'
                                          f'<div>{esc(str(e))}</div></div>'
                                          '<p><a href="/classes">返回班级列表</a></p>'),
                              HTTPStatus.BAD_REQUEST)

            def do_POST(self):
                path = urllib.parse.urlparse(self.path).path
                form = self._form()
                try:
                    app.dispatch_post(path, form)
                    back = form.get("back") or "/classes"
                    sep = "&" if "?" in back else "?"
                    self._redirect(f"{back}{sep}msg=done")
                except ServiceError as e:
                    back = form.get("back") or "/classes"
                    sep = "&" if "?" in back else "?"
                    self._redirect(f"{back}{sep}err=" + urllib.parse.quote(str(e)))

        return Handler

    # ---------- POST 分发 ----------
    def dispatch_post(self, path: str, f: dict):
        s = self.svc
        if path == "/action/pickup":
            s.pickup(f["bag_no"], proxy_code=(f.get("proxy_code") or None))
        elif path == "/action/confirm_return":
            if f.get("physically_seen") != "1":
                # 双重保险：界面勾框 + 服务端规则
                raise ServiceError("未见实物，不能标记送回", "not_seen")
            s.confirm_return(
                f["bag_no"], physically_seen=True,
                label_ok=f.get("label_ok") == "1",
                clean=f.get("clean") == "1",
                spare_used=f.get("spare_used") == "1",
                spare_note=f.get("spare_note", ""))
        elif path == "/action/monday_missing":
            s.monday_missing(f["bag_no"])
        elif path == "/action/label_off":
            s.mark_label_off(f["bag_no"], note=f.get("note", ""))
        elif path == "/action/damage":
            s.damage_replace(f["bag_no"], f.get("reason", "污损"))
        elif path == "/action/assign_spare":
            s.assign_spare(f["child_code"], f["spare_bag_no"],
                           reason=f.get("reason", "原袋污损/标签问题"))
        elif path == "/action/replacement":
            s.register_replacement(f["child_code"], f["bag_no"],
                                   f.get("material", "纯棉"),
                                   allergy_note=f.get("allergy_note", ""))
        elif path == "/action/transfer":
            s.transfer_child(f["child_code"], int(f["new_class_id"]),
                             f["new_slot_code"])
        elif path == "/action/mispick":
            inc_id = s.record_mispick(f["wrong_bag_no"], f["holder_child_code"],
                                      note=f.get("note", ""))
            return inc_id
        elif path == "/action/resolve_mispick":
            s.resolve_mispick(int(f["incident_id"]), exchanged=True)
        elif path == "/action/request_return":
            s.request_return(f["token"], note=f.get("note", ""))
        elif path == "/action/create_auth":
            s.create_authorization(f["token"], f["proxy_name"],
                                   relation=f.get("relation", ""),
                                   valid_days=int(f.get("days", "7")))
        elif path == "/action/revoke_auth":
            s.revoke_authorization(f["code"])
        else:
            raise ServiceError("未知操作", "not_found")

    # ---------- 页面 ----------
    def layout(self, title: str, inner: str) -> str:
        return (
            "<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{esc(title)} · 午睡袋管理</title>"
            f"<style>{CSS}</style>"
            '<header class="top"><span class="brand">幼儿园午睡袋管理</span>'
            '<a href="/classes">班级格位</a><a href="/incidents">特殊事项</a>'
            '<a href="/spares">备用袋库存</a>'
            '<span style="flex:1"></span>'
            '<a href="/guardian">家长端入口</a></header>'
            f"<main>{inner}</main>"
            "<footer>本地演示：数据保存在 SQLite，标签照片存本地，消息由模拟适配器返回</footer>"
            "</html>"
        )

    def _notices(self, q: dict) -> str:
        out = []
        if q.get("err"):
            out.append(f'<div class="notice err">{ICON_ALERT}<div>{esc(q["err"])}</div></div>')
        if q.get("msg"):
            out.append('<div class="notice ok">操作已完成，物品链已更新。</div>')
        return "".join(out)

    def page_classes(self, q: dict) -> str:
        cards = []
        for c in self.svc.list_classes():
            cards.append(
                f'<a class="panel btn" style="display:block;text-decoration:none" '
                f'href="/class?id={c["id"]}"><b style="font-size:16px">{esc(c["name"])}</b>'
                f'<div class="muted">{c["slot_rows"]} 行 × {c["slot_cols"]} 列格位</div></a>')
        inner = (f"<h1>班级格位</h1>{self._notices(q)}"
                 '<div class="grid">' + "".join(cards) + "</div>")
        return self.layout("班级格位", inner)

    def page_class(self, q: dict) -> str:
        class_id = int(q["id"])
        cls, slots = self.svc.class_board(class_id)
        pending = self.svc.pending_returns()
        open_inc = self.svc.list_incidents(open_only=True, class_id=class_id)

        alerts = ""
        mis = [i for i in open_inc if i["kind"] == "mispick"]
        if mis:
            items = "".join(
                f'<li><b>错拿：</b>{esc(i["bag_no"])} '
                f'<form class="inline" method="post" action="/action/resolve_mispick">'
                f'<input type="hidden" name="incident_id" value="{i["id"]}">'
                f'<input type="hidden" name="back" value="/class?id={class_id}">'
                '<button class="small primary">实物换回并核对</button></form>'
                f'<div class="muted">{esc(i["detail"])}</div></li>'
                for i in mis)
            alerts = f'<div class="notice">{ICON_ALERT}<div><b>本班有 {len(mis)} 件错拿待处理</b><ul>{items}</ul></div></div>'

        pend_rows = "".join(
            f'<tr><td>{esc(p["child_code"])}</td><td>{esc(p["bag_no"])}</td>'
            f'<td>{esc(p["class_name"])}</td></tr>' for p in pending) or \
            '<tr><td colspan="3" class="muted">暂无待核验送回</td></tr>'

        cells = []
        spare_count = 0
        for s in slots:
            head = (f'<div class="slot-head"><span class="slot-code">格位 {esc(s["label"])}'
                    "</span></div>")
            ch = s.get("child")
            if ch:
                status = ch["status"] or "stored"
                bag_line = (f'<div><span class="bag-no">{esc(ch["bag_no"])}</span> '
                            f'{badge(status)}</div>')
                if ch["is_spare"]:
                    bag_line += '<div class="muted">备用袋使用中</div>'
                if not ch["label_ok"]:
                    bag_line += '<div class="muted">⚠ 标签异常</div>'
                back = f"/class?id={class_id}"
                # 操作区：只有功能性按钮，无装饰图
                acts = []
                if status in ("stored", "in_use"):
                    acts.append(
                        f'<form class="inline" method="post" action="/action/pickup">'
                        f'<input type="hidden" name="bag_no" value="{esc(ch["bag_no"])}">'
                        f'<input type="hidden" name="back" value="{back}">'
                        f'<button class="small pickup">{ICON_PICKUP}领回清洗</button></form>')
                if status in ("taken_home", "return_pending"):
                    acts.append(
                        f'<details class="inline"><summary class="btn small primary">'
                        f'{ICON_RETURN}见实归位</summary>'
                        '<div class="panel" style="margin-top:8px;min-width:240px">'
                        f'<form method="post" action="/action/confirm_return">'
                        f'<input type="hidden" name="bag_no" value="{esc(ch["bag_no"])}">'
                        f'<input type="hidden" name="back" value="{back}">'
                        '<label class="seen-check"><input type="checkbox" name="physically_seen" value="1" required>'
                        "我已当面见到实物，并核对标签编号</label>"
                        '<div class="row" style="margin:6px 0">'
                        '<label class="field">标签'
                        '<select name="label_ok"><option value="1">完好</option>'
                        '<option value="0">异常/脱落</option></select></label>'
                        '<label class="field">清洁'
                        '<select name="clean"><option value="1">合格</option>'
                        '<option value="0">需返工</option></select></label></div>'
                        '<label class="field">本周备用袋'
                        '<select name="spare_used"><option value="0">未使用</option>'
                        '<option value="1">使用过</option></select></label>'
                        '<p class="muted">未见实物时此按钮不可提交。</p>'
                        '<button class="primary">确认送回归位</button></form></div></details>')
                    acts.append(
                        f'<form class="inline" method="post" action="/action/monday_missing">'
                        f'<input type="hidden" name="bag_no" value="{esc(ch["bag_no"])}">'
                        f'<input type="hidden" name="back" value="{back}">'
                        '<button class="small ghost" title="登记周一未带回">周一未带回</button></form>')
                acts.append(
                    f'<a class="btn small ghost" href="/bag?no={urllib.parse.quote(ch["bag_no"])}">物品链</a>')
                acts.append(
                    f'<form class="inline" method="post" action="/action/label_off">'
                    f'<input type="hidden" name="bag_no" value="{esc(ch["bag_no"])}">'
                    f'<input type="hidden" name="back" value="{back}">'
                    '<button class="small danger">标签脱落</button></form>')
                cells.append(
                    f'<div class="slot">{head}'
                    f'<div class="child-code">{esc(ch["code"])}</div>'
                    f'<div class="muted">{esc(ch["material"])}'
                    f'{" · 过敏：" + esc(ch["allergy_note"]) if ch["allergy_note"] else ""}</div>'
                    f'{bag_line}<div class="actions">{"".join(acts)}</div></div>')
            elif s.get("spare"):
                spare_count += 1
                sp = s["spare"]
                cells.append(
                    f'<div class="slot">{head}'
                    '<span class="badge spare">备用袋</span>'
                    f'<div class="bag-no">{esc(sp["bag_no"])}</div>'
                    f'<div class="muted">{esc(sp["material"])}</div>'
                    f'<div class="muted">库存中</div>'
                    f'<div class="actions"><a class="btn small ghost" href="/spares?id={class_id}">库存管理</a></div></div>')
            else:
                cells.append(
                    f'<div class="slot empty">{head}<div>空格位</div></div>')

        # 污损/无袋儿童的补袋与转班管理入口
        ops_panel = self._class_ops_panel(cls, slots)

        inner = (
            f'<h1>{esc(cls["name"])} · 格位板</h1>'
            f'{self._notices(q)}{alerts}'
            '<p class="muted">以儿童代号与格位号定位；领回＝下载图标“领回清洗”，'
            "送回＝勾选盾牌图标“见实归位”。</p>"
            '<div class="stat">'
            f'<div class="box">格位 <b>{len(slots)}</b></div>'
            f'<div class="box">备用袋在格 <b>{spare_count}</b></div>'
            f'<div class="box">待核验送回（全园）<b>{len(pending)}</b></div>'
            f'<div class="box">未结特殊事项 <b>{len(open_inc)}</b></div></div>'
            '<div class="panel"><h2 style="margin-top:0">待老师核验的送回</h2>'
            '<table><tr><th>儿童代号</th><th>睡袋编号</th><th>班级</th></tr>'
            f'{pend_rows}</table></div>'
            '<h2>格位明细</h2><div class="grid">' + "".join(cells) + "</div>"
            + ops_panel)
        return self.layout(cls["name"], inner)

    def _class_ops_panel(self, cls: dict, slots: list) -> str:
        class_id = cls["id"]
        classes = [c for c in self.svc.list_classes() if c["id"] != class_id]
        spares = self.svc.spare_stock()
        # 无在用好袋的孩子（污损/标签待查/无袋）
        needy = []
        for s in slots:
            ch = s.get("child")
            if not ch:
                continue
            if not ch.get("bag_no") or ch["status"] in ("damaged", "lost"):
                needy.append(ch)
        needy_html = "".join(
            f'<tr><td>{esc(ch["code"])}</td><td>{esc(ch.get("bag_no") or "—")}</td>'
            f'<td>{badge(ch["status"]) if ch["status"] else "无袋"}</td>'
            '<td><div class="row">'
            f'<form class="inline row" method="post" action="/action/assign_spare">'
            f'<input type="hidden" name="child_code" value="{esc(ch["code"])}">'
            f'<input type="hidden" name="back" value="/class?id={class_id}">'
            '<select name="spare_bag_no">'
            + "".join(f'<option value="{esc(sp["bag_no"])}">{esc(sp["bag_no"])}'
                      f'（{esc(sp["material"])}）</option>' for sp in spares)
            + '</select><button class="small primary">启用备用袋</button></form>'
            f'<form class="inline row" method="post" action="/action/replacement">'
            f'<input type="hidden" name="child_code" value="{esc(ch["code"])}">'
            f'<input type="hidden" name="back" value="/class?id={class_id}">'
            '<input type="text" name="bag_no" placeholder="新袋编号" required size="9">'
            '<input type="text" name="material" placeholder="材质" size="7">'
            '<button class="small">登记替换新袋</button></form>'
            '</div></td></tr>'
            for ch in needy)
        if needy:
            needy_html = ('<table><tr><th>儿童</th><th>原袋</th><th>状态</th>'
                          f'<th>补袋操作</th></tr>{needy_html}</table>')
        else:
            needy_html = '<p class="muted">没有待补袋的孩子。</p>'

        transfer_rows = []
        for s in slots:
            ch = s.get("child")
            if not ch or not ch.get("bag_no"):
                continue
            if classes:
                opts = ""
                for oc in classes:
                    _, oslots = self.svc.class_board(oc["id"])
                    free = [x for x in oslots
                            if not x["assigned_child_id"] and not x.get("spare")]
                    for x in free:
                        opts += (f'<option value="{oc["id"]}|{esc(x["code"])}">'
                                 f'{esc(oc["name"])} / {esc(x["label"])}</option>')
                transfer_rows.append(
                    f'<form class="inline row" method="post" action="/action/transfer">'
                    f'<input type="hidden" name="child_code" value="{esc(ch["code"])}">'
                    f'<input type="hidden" name="back" value="/class?id={class_id}">'
                    f'<b>{esc(ch["code"])}</b>（{esc(ch["bag_no"])}） → '
                    '<select name="target" onchange='
                    "'this.form.new_class_id.value=this.value.split(\"|\")[0];"
                    "this.form.new_slot_code.value=this.value.split(\"|\")[1]' >"
                    f'<option value="">选择目标班/格位</option>{opts}</select>'
                    '<input type="hidden" name="new_class_id">'
                    '<input type="hidden" name="new_slot_code">'
                    '<button class="small">转班交接</button></form>')
        transfer_html = ("<p class=\"muted\">没有其他班级可选。</p>" if not classes
                         else "<div class=\"row\">" + "".join(
                             f'<div>{r}</div>' for r in transfer_rows) + "</div>")

        # 错拿登记
        child_opts = "".join(
            f'<option value="{esc(s["child"]["code"])}">{esc(s["child"]["code"])}</option>'
            for s in slots if s.get("child"))
        mispick_html = (
            f'<form class="row" method="post" action="/action/mispick">'
            f'<input type="hidden" name="back" value="/class?id={class_id}">'
            '<label class="field">被错拿的袋号'
            '<input type="text" name="wrong_bag_no" required size="10"></label>'
            '<label class="field">错拿方儿童代号'
            f'<select name="holder_child_code">{child_opts}</select></label>'
            '<button class="small danger">登记错拿</button></form>')

        return (
            '<div class="panel"><h2 style="margin-top:0">污损 / 补袋</h2>'
            f'{needy_html}</div>'
            '<div class="panel"><h2 style="margin-top:0">转班交接</h2>'
            '<p class="muted">要求睡袋实物在校、标签完好；交接后格位与物品链一并更新。</p>'
            f'{transfer_html}</div>'
            '<div class="panel"><h2 style="margin-top:0">登记错拿</h2>'
            f'{mispick_html}</div>'
            '<div class="panel"><h2 style="margin-top:0">污损退役</h2>'
            '<form class="row" method="post" action="/action/damage">'
            f'<input type="hidden" name="back" value="/class?id={class_id}">'
            '<label class="field">袋号<input type="text" name="bag_no" size="10" required></label>'
            '<label class="field">原因<input type="text" name="reason" size="14" placeholder="如 汤汁浸透"></label>'
            '<button class="small danger">污损退役（随后启用备用袋）</button></form></div>'
        )

    def page_guardian(self, q: dict) -> str:
        token = q.get("token", "")
        if not token:
            inner = (
                "<h1>家长端</h1>" + self._notices(q) +
                '<div class="panel"><p>请输入老师提供的本人专属令牌：</p>'
                '<form class="row" method="get" action="/guardian">'
                '<input type="password" name="token" size="28" autofocus required>'
                '<button class="primary">查看我的睡袋</button></form>'
                '<p class="muted">家长端仅显示令牌对应孩子本人的睡袋状态。</p></div>')
            return self.layout("家长端", inner)

        view = self.svc.guardian_view(token)
        ch, bag, cls = view["child"], view["bag"], view["class"]
        bag_html = "<p>当前没有在管睡袋，请联系老师。</p>"
        actions = ""
        if bag:
            photo = (f'<p><a href="/label_photo?bag_no={urllib.parse.quote(bag["bag_no"])}">'
                     f'<img class="chain-photo" alt="{esc(bag["bag_no"])} 标签照片" '
                     f'src="/label_photo?bag_no={urllib.parse.quote(bag["bag_no"])}"></a></p>')
            st = badge(bag["status"])
            if bag["status"] == "taken_home":
                actions = (
                    '<div class="panel"><h2 style="margin-top:0">送回登记</h2>'
                    '<p class="muted">提交后状态变为“待老师核验”——老师见到实物核对标签与清洁后才会归位。</p>'
                    '<form method="post" action="/action/request_return" class="row">'
                    f'<input type="hidden" name="token" value="{esc(token)}">'
                    f'<input type="hidden" name="back" value="/guardian?token={esc(token)}">'
                    '<label class="field">备注（可选）<input type="text" name="note" size="26"></label>'
                    f'<button class="primary">{ICON_RETURN}已送交，待老师核验</button></form></div>')
            elif bag["status"] == "return_pending":
                actions = ('<div class="notice">睡袋已送到幼儿园，等待老师见到实物后核验归位。</div>')
            bag_html = (
                f'{photo}<p><span class="bag-no" style="font-size:17px;font-weight:700">'
                f'{esc(bag["bag_no"])}</span> {st}</p>'
                f'<p class="muted">材质：{esc(bag["material"])}'
                f'{" · 过敏注意：" + esc(bag["allergy_note"]) if bag["allergy_note"] else ""}'
                f'<br>班级：{esc(cls["name"])} · 标签：{"完好" if bag["label_ok"] else "异常待处理"}</p>')

        now = self.svc.now().isoformat(timespec="seconds")
        auth_rows = ""
        for a in view["authorizations"]:
            state, btn = "", ""
            if a["revoked_at"]:
                state = '<span class="badge damaged">已撤销（旧码立即失效）</span>'
            elif a["valid_until"] < now:
                state = '<span class="badge damaged">已过期</span>'
            else:
                state = (f'<span class="badge stored">有效至 {esc(a["valid_until"][:10])}'
                         f'</span> <span class="muted">已用 {a["used_count"]} 次</span>')
                btn = (f'<form class="inline" method="post" action="/action/revoke_auth">'
                       f'<input type="hidden" name="code" value="{esc(a["code"])}">'
                       f'<input type="hidden" name="back" value="/guardian?token={esc(token)}">'
                       '<button class="small danger">撤销授权</button></form>')
            auth_rows += (f'<tr><td><b>{esc(a["proxy_name"])}</b> '
                          f'<span class="muted">{esc(a["relation"])}</span></td>'
                          f'<td class="bag-no">{esc(a["code"])}</td><td>{state}</td>'
                          f'<td>{btn}</td></tr>')
        auth_rows = auth_rows or (
            '<tr><td colspan="4" class="muted">暂无授权</td></tr>')
        auth_panel = (
            '<div class="panel"><h2 style="margin-top:0">代领授权</h2>'
            '<form class="row" method="post" action="/action/create_auth">'
            f'<input type="hidden" name="token" value="{esc(token)}">'
            f'<input type="hidden" name="back" value="/guardian?token={esc(token)}">'
            '<label class="field">代领人姓名<input type="text" name="proxy_name" required size="10"></label>'
            '<label class="field">与孩子关系<input type="text" name="relation" size="10"></label>'
            '<label class="field">有效天数<input type="number" name="days" value="7" min="1" max="30" style="width:80px"></label>'
            '<button class="small primary">生成授权码</button></form>'
            '<p class="muted">授权码有有效期；撤销后旧码立即失效。</p>'
            '<table><tr><th>代领人</th><th>授权码</th><th>状态</th><th></th></tr>'
            f'{auth_rows}</table></div>')

        timeline = "".join(
            f'<li><span class="ts">{esc(e["ts"][:16].replace("T", " "))}</span>'
            f'<span class="tag">{esc(e["type"])}</span> {esc(e["note"])}'
            f'<div class="muted">操作人：{esc(e["actor"])}</div></li>'
            for e in view["events"]) or '<li class="muted">暂无记录</li>'

        inner = (
            f'<h1>家长端 · {esc(ch["code"])}</h1>{self._notices(q)}'
            f'<div class="panel"><h2 style="margin-top:0">我的睡袋</h2>{bag_html}</div>'
            f'{actions}{auth_panel}'
            f'<div class="panel"><h2 style="margin-top:0">最近物品链</h2>'
            f'<ul class="timeline">{timeline}</ul></div>'
            '<p><a href="/classes">老师端：班级格位</a></p>')
        return self.layout("家长端", inner)

    def page_spares(self, q: dict) -> str:
        class_id = int(q["id"]) if q.get("id") else None
        stock = self.svc.spare_stock()
        rows = "".join(
            f'<tr><td class="bag-no">{esc(b["bag_no"])}</td>'
            f'<td>{esc(b["material"])}</td><td>{esc(b.get("class_id"))}</td>'
            f'<td>{esc(b["allergy_note"] or "—")}</td></tr>' for b in stock) \
            or '<tr><td colspan="4" class="muted">暂无库存</td></tr>'
        counts = {}
        for b in stock:
            counts[b["class_id"]] = counts.get(b["class_id"], 0) + 1
        stat = "".join(f'<div class="box">班级 {esc(cid)}：<b>{n}</b> 个</div>'
                       for cid, n in sorted(counts.items()))
        inner = (f"<h1>备用袋库存</h1>{self._notices(q)}"
                 f'<div class="stat">{stat or "<div class=muted>库存为空</div>"}</div>'
                 '<div class="panel"><table><tr><th>编号</th><th>材质</th>'
                 f'<th>班级</th><th>过敏注意</th></tr>{rows}</table></div>')
        return self.layout("备用袋库存", inner)

    def page_incidents(self, q: dict) -> str:
        rows = ""
        for i in self.svc.list_incidents():
            status = ('<span class="badge stored">已处理</span>' if i["resolved_at"]
                      else '<span class="badge return_pending">待处理</span>')
            detail = esc(i["detail"])
            if i["kind"] == "mispick":
                # 错拿 detail 是 JSON，展示成可读文本
                import json
                try:
                    info = json.loads(i["detail"])
                    detail = (f'袋 {esc(info["wrong_bag_no"])}（属 {esc(info["owner_child_code"])}）'
                              f'被 {esc(info["holder_child_code"])} 家庭错拿')
                except Exception:
                    pass
            rows += (f'<tr><td>{i["id"]}</td><td>{esc(INCIDENT_TEXT.get(i["kind"], i["kind"]))}'
                     f'</td><td>{esc(i["child_code"])}</td><td>{esc(i["bag_no"])}</td>'
                     f'<td>{detail}</td><td>{status}</td>'
                     f'<td class="muted">{esc(i["created_at"][:16].replace("T", " "))}</td></tr>')
        inner = (f"<h1>特殊事项</h1>{self._notices(q)}"
                 '<p class="muted">代领、标签脱落、错拿、周一未带回、污损更换、转班均单独记录。</p>'
                 '<div class="panel"><table><tr><th>#</th><th>类型</th><th>儿童</th>'
                 f'<th>睡袋</th><th>详情</th><th>状态</th><th>时间</th></tr>{rows}</table></div>')
        return self.layout("特殊事项", inner)

    def page_bag(self, q: dict) -> str:
        chain = self.svc.bag_chain(q["no"])
        b = chain["bag"]
        timeline = "".join(
            f'<li><span class="ts">{esc(e["ts"][:16].replace("T", " "))}</span>'
            f'<span class="tag">{esc(e["type"])}</span> {esc(e["note"])}'
            f'<div class="muted">操作人：{esc(e["actor"])}'
            + (f' · 儿童：{esc(e["child_code"])}' if e["child_code"] else "") + '</div></li>'
            for e in chain["events"])
        photos = "".join(
            f'<figure><img class="chain-photo" alt="标签照片" src="/label_photo?bag_no={urllib.parse.quote(b["bag_no"])}">'
            f'<figcaption class="muted">{esc(p["event_type"])} · {esc(p["ts"][:16])}</figcaption></figure>'
            for p in chain["photos"]) or \
            f'<a href="/label_photo?bag_no={urllib.parse.quote(b["bag_no"])}">查看标签照片</a>'
        spare_badge = '<span class="badge spare">备用袋</span>' if b["is_spare"] else ""
        inner = (
            f'<h1>物品链 · {esc(b["bag_no"])}</h1>{self._notices(q)}'
            f'<div class="panel"><p>{badge(b["status"])} {spare_badge} 儿童代号：<b>{esc(b["child_code"] or "—")}</b> · '
            f'格位：{esc(b["slot_code"] or "—")}（{esc(b["class_name"] or "—")}）</p>'
            f'<p class="muted">材质 {esc(b["material"])} · 过敏 {esc(b["allergy_note"] or "无")} · '
            f'标签 {"完好" if b["label_ok"] else "异常"}</p>{photos}</div>'
            f'<div class="panel"><ul class="timeline">{timeline}</ul></div>'
            f'<p><a href="/classes">返回班级</a></p>')
        return self.layout("物品链", inner)

    def serve_photo(self, handler, q: dict):
        bag_no = q.get("bag_no", "")
        chain = None
        path = None
        try:
            chain = self.svc.bag_chain(bag_no)
            if chain["photos"]:
                path = chain["photos"][-1]["path"]
        except ServiceError:
            pass
        if path and os.path.exists(path):
            with open(path, "rb") as f:
                data = f.read()
        else:
            # 未存过照片时即时生成一张（不落盘的兜底）
            data = render_label_png(bag_no)
        handler._send(200, data, "image/png")


def serve(service: NapService, photo_dir: str, host: str = "127.0.0.1",
          port: int = 8000) -> ThreadingHTTPServer:
    app = NapWebApp(service, photo_dir)
    httpd = ThreadingHTTPServer((host, port), app.make_handler())
    return httpd
