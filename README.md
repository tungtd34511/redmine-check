# Redmine Daily Check – chạy tự động trên GitHub Actions

Không cần bật laptop. GitHub tự chạy thứ 2 – thứ 6 và gửi Telegram:

**9h sáng – báo cáo ngày** (`redmine-check.yml`)
- Log time ngày làm việc trước đã đủ 8h chưa (thứ 2 thì kiểm tra thứ 6 tuần trước)
- Giờ log từng ngày của tuần này và tuần trước
- Task chưa hoàn thành **được tạo trong tuần này hoặc tuần trước**, kèm trạng thái, % và hạn

**Mỗi giờ từ 8h đến 17h – kiểm tra bug** (`redmine-bugs.yml`)
- Bug chưa hoàn thành **được tạo trong tuần này hoặc tuần trước**, kèm trạng thái và độ ưu tiên
- Bug mới tạo trong giờ vừa qua được đánh dấu 🆕

## 1. Lấy thông tin cần thiết
- **Redmine API key**: vào https://redmine.dbvi.com.vn/my/account → khung bên phải *API access key* → *Show*.
- **Telegram bot token**: chat với `@BotFather` → `/newbot` → đặt tên → nhận token.
- **Telegram chat_id**: nhắn một tin bất kỳ cho bot vừa tạo, rồi mở
  `https://api.telegram.org/bot<TOKEN>/getUpdates` → tìm `"chat":{"id": ...}`.

## 2. Tạo repository trên GitHub
1. Đăng nhập https://github.com (tạo tài khoản miễn phí nếu chưa có).
2. **New repository** → đặt tên, ví dụ `redmine-check` → chọn **Private** → **Create**.
3. Bấm **uploading an existing file** và tải lên:
   - `redmine_check.py`
   - `config.json`
   - file workflow phải nằm đúng đường dẫn `.github/workflows/redmine-check.yml`.
     Cách dễ nhất: bấm **Add file → Create new file**, gõ tên `.github/workflows/redmine-check.yml`,
     rồi dán nội dung file `redmine-check.yml` vào.

## 3. Thêm Secrets (thông tin bí mật)
Repository → **Settings → Secrets and variables → Actions → New repository secret**, tạo 4 secret:

| Tên | Giá trị |
|---|---|
| `REDMINE_URL` | `https://redmine.dbvi.com.vn` |
| `REDMINE_API_KEY` | API key ở bước 1 |
| `TELEGRAM_BOT_TOKEN` | Token bot |
| `TELEGRAM_CHAT_ID` | chat_id |

Secrets được mã hóa, không ai xem lại được, kể cả bạn. Không ghi các giá trị này vào `config.json`.

## 4. Chạy thử
Tab **Actions** → **Redmine Daily Check** → **Run workflow**. Sau khoảng 30 giây bạn sẽ nhận tin Telegram.
Nếu lỗi: bấm vào lần chạy màu đỏ để xem log. Bạn cũng sẽ nhận tin báo lỗi qua Telegram nếu bot đã cấu hình đúng.

## 5. Tùy chỉnh (sửa `config.json` trên GitHub)
| Khóa | Ý nghĩa |
|---|---|
| `required_hours_per_day` | Số giờ cần log mỗi ngày |
| `task_trackers` | Tên tracker tính là task, đúng như trên Redmine |
| `bug_trackers` | Tên tracker tính là bug |
| `bug_notify` | `"all"`: mỗi giờ báo nếu còn bug mở · `"new"`: chỉ báo khi có bug mới |
| `exclude_statuses` | Trạng thái coi như đã xong, không báo (vd. `"Closed"`, `"Rejected"`, `"Resolved"`) |
| `holidays` | Ngày nghỉ lễ / nghỉ phép (`YYYY-MM-DD`), các ngày này không bị kiểm tra |

## Lưu ý
- "Chưa hoàn thành" = trạng thái đang mở trên Redmine (chưa Closed/Rejected...).
- Lịch của GitHub thường trễ 5–15 phút, nên đã đặt chạy lúc 8:50 để tin nhắn tới khoảng 9h.
- GitHub tự **tạm dừng lịch chạy sau 60 ngày repository không có thay đổi** và gửi email báo trước.
  Khi nhận email, bấm nút kích hoạt lại, hoặc sửa nhẹ một file (ví dụ thêm ngày nghỉ vào `holidays`).
- Nếu sau này Redmine chỉ cho truy cập từ IP công ty, GitHub sẽ không vào được. Khi đó quay về cách chạy trên máy trong công ty (`install_task.ps1`).
