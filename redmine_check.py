#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redmine check – thứ 2 đến thứ 6, gửi kết quả qua Telegram.

  --mode daily (9h sáng):
      1. Ngày làm việc trước đã log đủ giờ chưa (thứ 2 thì kiểm tra thứ 6 tuần trước)
      2. Giờ log từng ngày của tuần này và tuần trước
      3. Task chưa hoàn thành, TẠO trong tuần này hoặc tuần trước, kèm trạng thái

  --mode bugs (mỗi giờ, giờ hành chính):
      Bug chưa hoàn thành, TẠO trong tuần này hoặc tuần trước, kèm trạng thái.
      Bug mới tạo trong giờ vừa qua được đánh dấu 🆕.

Chỉ dùng thư viện chuẩn Python 3.8+.
Chạy thử:  python redmine_check.py --mode daily --dry-run
           python redmine_check.py --mode bugs  --dry-run --date 2026-10-13
"""
import argparse
import datetime as dt
import html
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
LOG_PATH = os.path.join(BASE_DIR, "redmine_check.log")
VN_TZ = dt.timezone(dt.timedelta(hours=7))
WEEKDAYS_VI = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "CN"]

ENV_KEYS = {  # biến môi trường (GitHub Secrets) ghi đè config.json
    "REDMINE_URL": "redmine_url",
    "REDMINE_API_KEY": "api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}


# ----------------------------------------------------------------- helpers
def log(msg):
    line = f"[{dt.datetime.now(VN_TZ):%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    for env, key in ENV_KEYS.items():
        if os.environ.get(env):
            cfg[key] = os.environ[env].strip()
    missing = [k for k in ENV_KEYS.values() if not cfg.get(k)]
    if missing:
        sys.exit(f"Thiếu cấu hình: {', '.join(missing)} (điền vào config.json hoặc GitHub Secrets).")
    cfg.setdefault("required_hours_per_day", 8)
    cfg.setdefault("task_trackers", ["Task"])
    cfg.setdefault("bug_trackers", ["Bug"])
    cfg.setdefault("bug_notify", "all")     # "all": báo mỗi giờ nếu còn bug mở; "new": chỉ báo khi có bug mới
    cfg.setdefault("exclude_statuses", ["Closed", "Rejected", "Reject"])
    cfg.setdefault("holidays", [])
    cfg.setdefault("verify_ssl", True)
    cfg["redmine_url"] = cfg["redmine_url"].rstrip("/")
    return cfg


def fmt_d(d):
    return f"{WEEKDAYS_VI[d.weekday()]} {d:%d/%m}"


def parse_ts(s):
    """Redmine trả created_on dạng '2026-10-08T03:12:00Z' (UTC) -> datetime giờ VN."""
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(VN_TZ)


# ----------------------------------------------------------------- Redmine API
class Redmine:
    def __init__(self, url, api_key, verify_ssl=True):
        self.url = url
        self.api_key = api_key
        self.ctx = None if verify_ssl else ssl._create_unverified_context()

    def get(self, path, params=None):
        q = "?" + urllib.parse.urlencode(params, doseq=True) if params else ""
        req = urllib.request.Request(
            f"{self.url}{path}{q}",
            headers={"X-Redmine-API-Key": self.api_key, "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=60, context=self.ctx) as r:
            return json.loads(r.read().decode("utf-8"))

    def get_all(self, path, key, params):
        items, offset = [], 0
        while True:
            data = self.get(path, dict(params, limit=100, offset=offset))
            batch = data.get(key, [])
            items.extend(batch)
            offset += len(batch)
            if not batch or offset >= data.get("total_count", 0):
                return items


# ----------------------------------------------------------------- logic
def working_days(start, end, holidays):
    d, out = start, []
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def previous_working_day(today, holidays):
    d = today - dt.timedelta(days=1)
    while d.weekday() >= 5 or d.isoformat() in holidays:
        d -= dt.timedelta(days=1)
    return d


def hours_by_day(rm, user_id, start, end):
    entries = rm.get_all("/time_entries.json", "time_entries",
                         {"user_id": user_id, "from": start.isoformat(), "to": end.isoformat()})
    totals = {}
    for e in entries:
        totals[e["spent_on"]] = totals.get(e["spent_on"], 0.0) + float(e["hours"])
    return totals


def tracker_ids(rm, names):
    wanted = {n.strip().lower() for n in names}
    all_t = rm.get("/trackers.json").get("trackers", [])
    ids = [str(t["id"]) for t in all_t if t["name"].strip().lower() in wanted]
    if not ids:
        avail = ", ".join(t["name"] for t in all_t)
        raise SystemExit(f"Không tìm thấy tracker {names}. Tracker hiện có: {avail}")
    return ids


def open_issues_created_since(rm, user_id, t_ids, since, exclude_statuses):
    """Issue chưa đóng, giao cho mình, tạo từ ngày `since` trở đi.
    Loại thêm các trạng thái trong exclude_statuses, vì một số Redmine
    không đánh dấu Closed/Rejected là trạng thái đóng."""
    issues = rm.get_all("/issues.json", "issues", {
        "assigned_to_id": user_id,
        "status_id": "open",
        "tracker_id": "|".join(t_ids),
        "created_on": f">={since.isoformat()}",
        "sort": "created_on:desc",
    })
    excl = {s.strip().lower() for s in exclude_statuses}
    return [i for i in issues if i["status"]["name"].strip().lower() not in excl]


def split_by_created_week(issues, this_mon, last_mon):
    this_w, last_w = [], []
    for i in issues:
        c = parse_ts(i["created_on"]).date()
        if c >= this_mon:
            this_w.append(i)
        elif c >= last_mon:
            last_w.append(i)
    return this_w, last_w


# ----------------------------------------------------------------- report
LINE = "━━━━━━━━━━━━━━━━━━"
WD_SHORT = ["T2", "T3", "T4", "T5", "T6", "T7", "CN"]
STATUS_ICON = {"new": "🆕", "open": "⚪", "in progress": "🔄", "doing": "🔄", "feedback": "💬",
               "reopened": "🔁", "reopen": "🔁", "resolved": "✔️", "pending": "⏸", "testing": "🧪"}
PRIO_ICON = {"immediate": "🔴", "urgent": "🔴", "high": "🟠", "normal": "🟡", "low": "🟢"}


def progress_bar(pct, width=5):
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def issue_block(n, i, base_url, today, new_since=None):
    """
    1. #2406 Lỗi sai logic phân cấp tạm ứng 🆕
       🔄 In Progress · ▰▰▱▱▱ 40%
       🟠 High · tạo 12/10 · ⏰ hạn 14/10
    """
    is_bug = i["tracker"]["name"].lower() == "bug"
    created = parse_ts(i["created_on"])
    new = " 🆕" if new_since and created >= new_since else ""
    subj = html.escape(i["subject"].strip()[:70] + ("…" if len(i["subject"].strip()) > 70 else ""))
    status = i["status"]["name"]
    s_icon = STATUS_ICON.get(status.lower(), "•")
    pct = i.get("done_ratio", 0) or 0
    meta = []
    if is_bug:
        prio = i.get("priority", {}).get("name", "")
        if prio:
            meta.append(f"{PRIO_ICON.get(prio.lower(), '⚪')} {html.escape(prio)}")
    meta.append(f"tạo {created:%d/%m}")
    due = i.get("due_date")
    if due:
        d = dt.date.fromisoformat(due)
        meta.append(f"<b>⚠️ trễ {(today - d).days} ngày</b>" if d < today else f"⏰ hạn {d:%d/%m}")
    return (f'<b>{n}.</b> <a href="{base_url}/issues/{i["id"]}">#{i["id"]}</a> {subj}{new}\n'
            f'    {s_icon} <i>{html.escape(status)}</i> · {progress_bar(pct)} {pct}%\n'
            f'    {" · ".join(meta)}')


def issue_sections(this_w, last_w, url, today, new_since=None, sort_key=None):
    out, n = [], 0
    for title, items in (("🔹 Tạo tuần này", this_w), ("🔸 Tạo tuần trước", last_w)):
        if not items:
            continue
        if sort_key:
            items = sorted(items, key=sort_key)
        out.append(f"\n<b>{title}</b> ({len(items)})")
        for i in items:
            n += 1
            out.append(issue_block(n, i, url, today, new_since))
    return out


def week_line(days, req):
    """T2 ✅8  T3 ✅8  T4 ⚠️5  T5 ❌0"""
    parts = []
    for d, h in days:
        mark = "✅" if h >= req else ("❌" if h == 0 else "⚠️")
        parts.append(f"{WD_SHORT[d.weekday()]} {mark}{h:g}")
    return "  ".join(parts)


def build_daily(cfg, today, user, prev_day, prev_hours, week_hours, tasks_this, tasks_last):
    req = cfg["required_hours_per_day"]
    url = cfg["redmine_url"]
    out = [f"📋 <b>BÁO CÁO NGÀY · {fmt_d(today)}</b>", f"👤 {html.escape(user)}", LINE]

    # Log time
    out.append("⏱ <b>LOG TIME</b>")
    if prev_hours >= req:
        out.append(f"{fmt_d(prev_day)}: <b>{prev_hours:g}/{req:g}h</b> ✅ Đủ")
    else:
        out.append(f"{fmt_d(prev_day)}: <b>{prev_hours:g}/{req:g}h</b> ❌ <b>Thiếu {req - prev_hours:g}h</b>")
        out.append(f'👉 <a href="{url}/time_entries/new">Log time ngay</a>')
    for title, days in week_hours:
        if not days:
            continue
        total, need = sum(h for _, h in days), req * len(days)
        mark = "✅" if total >= need else "⚠️"
        out.append(f"\n<b>{title}</b> {total:g}/{need:g}h {mark}")
        out.append(week_line(days, req))

    # Task
    n = len(tasks_this) + len(tasks_last)
    out.append(LINE)
    out.append(f"📌 <b>TASK ĐANG MỞ · {n}</b>")
    if n == 0:
        out.append("Không có task tồn đọng 🎉")
    else:
        late_first = lambda i: (i.get("due_date") or "9999-12-31")
        out.extend(issue_sections(tasks_this, tasks_last, url, today, sort_key=late_first))
    return "\n".join(out)


def build_bugs(cfg, now, bugs_this, bugs_last, new_since):
    n = len(bugs_this) + len(bugs_last)
    n_new = sum(1 for i in bugs_this + bugs_last if parse_ts(i["created_on"]) >= new_since)
    head = f"🐞 <b>BUG ĐANG MỞ · {n}</b>" + (f"   🆕 <b>{n_new} mới</b>" if n_new else "")
    out = [head, f"🕘 {now:%H:%M} · {fmt_d(now.date())}", LINE]
    if n == 0:
        out.append("Không có bug nào đang mở 🎉")
    else:
        # ưu tiên cao lên trước, rồi bug mới nhất
        by_prio = lambda i: (-(i.get("priority", {}).get("id", 0)), -parse_ts(i["created_on"]).timestamp())
        out.extend(issue_sections(bugs_this, bugs_last, cfg["redmine_url"], now.date(),
                                  new_since, sort_key=by_prio))
    return "\n".join(out), n, n_new


# ----------------------------------------------------------------- telegram
def send_telegram(token, chat_id, text):
    chunks, cur = [], ""
    for line in text.split("\n"):           # Telegram giới hạn 4096 ký tự / tin
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        body = urllib.parse.urlencode({
            "chat_id": chat_id, "text": c, "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }).encode()
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30).read()


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "bugs"], default="daily")
    ap.add_argument("--dry-run", action="store_true", help="Chỉ in báo cáo, không gửi Telegram")
    ap.add_argument("--date", help="Giả lập ngày chạy (YYYY-MM-DD)")
    ap.add_argument("--force", action="store_true", help="Chạy cả cuối tuần / ngày lễ")
    ap.add_argument("--always", action="store_true", help="Luôn gửi tin (dùng khi bấm chạy tay)")
    args = ap.parse_args()

    cfg = load_config()
    now = dt.datetime.now(VN_TZ)
    if args.date:
        now = dt.datetime.combine(dt.date.fromisoformat(args.date), now.timetz())
    today = now.date()
    holidays = set(cfg["holidays"])

    if not args.force and (today.weekday() >= 5 or today.isoformat() in holidays):
        log(f"{today} là ngày nghỉ, bỏ qua.")
        return

    rm = Redmine(cfg["redmine_url"], cfg["api_key"], cfg["verify_ssl"])
    me = rm.get("/users/current.json")["user"]
    user_id = me["id"]
    user_name = f'{me.get("firstname", "")} {me.get("lastname", "")}'.strip() or me.get("login", "")

    this_mon = today - dt.timedelta(days=today.weekday())
    last_mon = this_mon - dt.timedelta(days=7)
    last_fri = last_mon + dt.timedelta(days=4)

    def send(text):
        if args.dry_run:
            print(text)
        else:
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], text)

    if args.mode == "bugs":
        bugs = open_issues_created_since(rm, user_id, tracker_ids(rm, cfg["bug_trackers"]), last_mon,
                                          cfg["exclude_statuses"])
        b_this, b_last = split_by_created_week(bugs, this_mon, last_mon)
        new_since = now - dt.timedelta(minutes=65)
        text, n, n_new = build_bugs(cfg, now, b_this, b_last, new_since)
        notify = args.always or (n_new > 0 if cfg["bug_notify"] == "new" else n > 0)
        if notify:
            send(text)
        log(f"[bugs] {n} bug mở, {n_new} mới, {'đã gửi' if notify else 'không gửi'}.")
        return

    # daily
    prev_day = previous_working_day(today, holidays)
    totals = hours_by_day(rm, user_id, min(last_mon, prev_day), today)
    prev_hours = totals.get(prev_day.isoformat(), 0.0)
    yesterday = today - dt.timedelta(days=1)
    week_hours = [
        ("📅 Tuần này", [(d, totals.get(d.isoformat(), 0.0))
                                    for d in working_days(this_mon, yesterday, holidays)]),
        ("📅 Tuần trước", [(d, totals.get(d.isoformat(), 0.0))
                        for d in working_days(last_mon, last_fri, holidays)]),
    ]
    tasks = open_issues_created_since(rm, user_id, tracker_ids(rm, cfg["task_trackers"]), last_mon,
                                       cfg["exclude_statuses"])
    t_this, t_last = split_by_created_week(tasks, this_mon, last_mon)
    send(build_daily(cfg, today, user_name, prev_day, prev_hours, week_hours, t_this, t_last))
    log(f"[daily] {prev_day} = {prev_hours:g}h, {len(tasks)} task mở.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # báo lỗi về Telegram để không bị "im lặng"
        log(f"LỖI: {e!r}")
        try:
            cfg = load_config()
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                          f"⚠️ Redmine check lỗi: {html.escape(repr(e))}")
        except Exception:
            pass
        sys.exit(1)
