#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Redmine daily check (thứ 2 - thứ 6, 9h sáng)
  1. Ngày làm việc hôm trước đã log đủ giờ chưa (thứ 2 thì kiểm tra thứ 6 tuần trước)
  2. Tổng hợp log time từng ngày trong tuần này
  3. Task / Bug chưa hoàn thành của tuần trước và tuần này (theo due date), kèm task quá hạn
  4. Gửi kết quả qua Telegram

Chỉ dùng thư viện chuẩn của Python 3.8+, không cần pip install.
Chạy thử:  python redmine_check.py --dry-run      (in ra màn hình, không gửi Telegram)
           python redmine_check.py --date 2026-10-12   (giả lập chạy vào ngày đó)
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
WEEKDAYS_VI = ["Thứ 2", "Thứ 3", "Thứ 4", "Thứ 5", "Thứ 6", "Thứ 7", "CN"]


# ----------------------------------------------------------------- helpers
def log(msg):
    line = f"[{dt.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}"
    print(line)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


ENV_KEYS = {  # biến môi trường (GitHub Secrets) ghi đè config.json
    "REDMINE_URL": "redmine_url",
    "REDMINE_API_KEY": "api_key",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_CHAT_ID": "telegram_chat_id",
}


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
    cfg.setdefault("trackers", ["Task", "Bug"])
    cfg.setdefault("holidays", [])
    cfg.setdefault("verify_ssl", True)
    cfg.setdefault("include_no_due_date", True)
    cfg["redmine_url"] = cfg["redmine_url"].rstrip("/")
    return cfg


def fmt_d(d):
    return f"{WEEKDAYS_VI[d.weekday()]} {d:%d/%m}"


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
            p = dict(params, limit=100, offset=offset)
            data = self.get(path, p)
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


def open_issues(rm, user_id, t_ids):
    return rm.get_all("/issues.json", "issues", {
        "assigned_to_id": user_id,
        "status_id": "open",
        "tracker_id": "|".join(t_ids),
        "sort": "due_date",
    })


def classify_issues(issues, today, last_mon, last_fri, this_mon, this_fri):
    groups = {"overdue_old": [], "last_week": [], "this_week": [], "no_due": []}
    for i in issues:
        due = i.get("due_date")
        start = i.get("start_date")
        if due:
            d = dt.date.fromisoformat(due)
            if d < last_mon:
                groups["overdue_old"].append(i)
            elif d <= last_fri + dt.timedelta(days=2):
                groups["last_week"].append(i)
            elif d <= this_fri + dt.timedelta(days=2):
                groups["this_week"].append(i)
        else:
            # Không có due date: đưa vào nếu start date nằm trong tuần trước / tuần này hoặc không có start date
            if start:
                s = dt.date.fromisoformat(start)
                if s <= this_fri + dt.timedelta(days=2):
                    groups["no_due"].append(i)
            else:
                groups["no_due"].append(i)
    return groups


# ----------------------------------------------------------------- report
def issue_line(i, base_url, today):
    tracker = i["tracker"]["name"]
    icon = "🐞" if tracker.lower() == "bug" else "📌"
    subj = html.escape(i["subject"][:80])
    status = html.escape(i["status"]["name"])
    done = i.get("done_ratio", 0)
    due = i.get("due_date")
    late = ""
    if due and dt.date.fromisoformat(due) < today:
        late = f" ⚠️ trễ {(today - dt.date.fromisoformat(due)).days} ngày"
    due_txt = f" | hạn {dt.date.fromisoformat(due):%d/%m}" if due else ""
    return (f'{icon} <a href="{base_url}/issues/{i["id"]}">#{i["id"]}</a> {subj}\n'
            f'     {status} · {done}%{due_txt}{late}')


def build_report(cfg, today, user, prev_day, prev_hours, week_hours, groups):
    req = cfg["required_hours_per_day"]
    url = cfg["redmine_url"]
    out = [f"<b>📋 Redmine check – {fmt_d(today)}/{today:%Y}</b>",
           f"👤 {html.escape(user)}", ""]

    # 1. Ngày hôm trước
    ok = prev_hours >= req
    out.append("<b>⏱ Log time ngày làm việc trước</b>")
    out.append(f"{fmt_d(prev_day)}: <b>{prev_hours:g}h / {req:g}h</b> "
               + ("✅ Đủ" if ok else f"❌ Thiếu {req - prev_hours:g}h"))
    if not ok:
        out.append(f'👉 <a href="{url}/time_entries/new">Log time ngay</a>')
    out.append("")

    # 2. Tuần này + tuần trước
    for title, days in week_hours:
        if not days:
            continue
        total = sum(h for _, h in days)
        need = req * len(days)
        out.append(f"<b>📅 {title}: {total:g}h / {need:g}h</b>")
        for d, h in days:
            mark = "✅" if h >= req else ("❌" if h == 0 else "⚠️")
            out.append(f"  {mark} {fmt_d(d)}: {h:g}h")
        out.append("")

    # 3. Issues
    sections = [
        ("🔴 Quá hạn từ trước tuần trước", "overdue_old"),
        ("🟠 Tuần trước chưa xong", "last_week"),
        ("🟡 Tuần này chưa xong", "this_week"),
    ]
    if cfg["include_no_due_date"]:
        sections.append(("⚪ Chưa có due date", "no_due"))
    total_open = sum(len(groups[k]) for _, k in sections)
    out.append(f"<b>🧩 Task/Bug chưa hoàn thành: {total_open}</b>")
    if total_open == 0:
        out.append("Không có task/bug nào tồn đọng 🎉")
    for title, key in sections:
        items = groups[key]
        if not items:
            continue
        out.append(f"\n<b>{title} ({len(items)})</b>")
        out.extend(issue_line(i, url, today) for i in items)
    return "\n".join(out)


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
    ap.add_argument("--dry-run", action="store_true", help="Chỉ in báo cáo, không gửi Telegram")
    ap.add_argument("--date", help="Giả lập ngày chạy (YYYY-MM-DD)")
    ap.add_argument("--force", action="store_true", help="Chạy cả cuối tuần / ngày lễ")
    args = ap.parse_args()

    cfg = load_config()
    vn_now = dt.datetime.now(dt.timezone(dt.timedelta(hours=7)))  # giờ Việt Nam, kể cả khi máy chạy UTC
    today = dt.date.fromisoformat(args.date) if args.date else vn_now.date()
    holidays = set(cfg["holidays"])

    if not args.force and (today.weekday() >= 5 or today.isoformat() in holidays):
        log(f"{today} là ngày nghỉ, bỏ qua.")
        return

    rm = Redmine(cfg["redmine_url"], cfg["api_key"], cfg["verify_ssl"])
    me = rm.get("/users/current.json")["user"]
    user_id = me["id"]
    user_name = f'{me.get("firstname", "")} {me.get("lastname", "")}'.strip() or me.get("login", "")

    this_mon = today - dt.timedelta(days=today.weekday())
    this_fri = this_mon + dt.timedelta(days=4)
    last_mon = this_mon - dt.timedelta(days=7)
    last_fri = last_mon + dt.timedelta(days=4)
    prev_day = previous_working_day(today, holidays)

    totals = hours_by_day(rm, user_id, min(last_mon, prev_day), today)
    prev_hours = totals.get(prev_day.isoformat(), 0.0)

    yesterday = today - dt.timedelta(days=1)
    week_hours = [
        ("Tuần này (đến hôm qua)", [(d, totals.get(d.isoformat(), 0.0))
                                    for d in working_days(this_mon, yesterday, holidays)]),
        ("Tuần trước", [(d, totals.get(d.isoformat(), 0.0))
                        for d in working_days(last_mon, last_fri, holidays)]),
    ]

    issues = open_issues(rm, user_id, tracker_ids(rm, cfg["trackers"]))
    groups = classify_issues(issues, today, last_mon, last_fri, this_mon, this_fri)

    report = build_report(cfg, today, user_name, prev_day, prev_hours, week_hours, groups)

    if args.dry_run:
        print(report)
    else:
        send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"], report)
        log(f"Đã gửi báo cáo: {prev_day} = {prev_hours:g}h, {len(issues)} issue mở.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:  # gửi lỗi về Telegram để không bị "im lặng"
        log(f"LỖI: {e!r}")
        try:
            cfg = load_config()
            send_telegram(cfg["telegram_bot_token"], cfg["telegram_chat_id"],
                          f"⚠️ Redmine check lỗi: {html.escape(repr(e))}\n(Có thể chưa bật VPN?)")
        except Exception:
            pass
        sys.exit(1)
