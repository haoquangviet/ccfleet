# ccfleet

**Trình quản lý phiên cho Claude Code — chạy và quản lý nhiều phiên Claude Code trên Windows, có giao diện web dùng được trên điện thoại.**

English: [README.md](README.md)

**ccfleet** là một supervisor nhỏ viết bằng Python (thư viện chuẩn + Pillow) và không cần VS Code. Nó chạy nhiều phiên
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) cùng lúc, mỗi phiên trong một cửa sổ console riêng, có bật
Remote Control. Phiên nào thoát thì ccfleet tự mở lại, và sau khi máy khởi động lại vẫn tiếp tục đúng hội thoại cũ.
Bạn điều khiển tất cả từ menu trong terminal hoặc từ giao diện web trên điện thoại và máy tính.
ccfleet hỗ trợ nhiều tài khoản Claude: chọn tài khoản cho từng phiên và xem mức dùng của từng tài khoản.

> Không liên kết với Anthropic. Claude và Claude Code là nhãn hiệu của Anthropic.

## Tính năng

- **Supervisor**
  - Mỗi phiên chạy trong console riêng với `--remote-control`.
  - Phiên thoát thì tự mở lại, có giãn cách khi lỗi lặp lại liên tục. Khi mở lại, phiên dùng `--resume` để tiếp tục đúng hội thoại.
  - Tự đồng ý hộp thoại "trust this folder".
  - Lịch gửi prompt định kỳ, chỉ gửi khi phiên rảnh.
  - Tự chạy `claude update` vào ban đêm.
  - Không mở phiên mới khi máy thiếu RAM.
  - Giới hạn RAM/CPU cho từng phiên bằng Windows Job Objects (áp dụng cho cả tiến trình con).
- **Menu terminal:** gõ `ccfleet` để mở. Chọn số phiên rồi bấm một phím để xem màn hình, gửi prompt, chạy, dừng hoặc khởi động lại.
- **Giao diện web (điện thoại + máy tính)**
  - Xem hội thoại trực tiếp; khi Claude đang làm sẽ hiện dòng trạng thái.
  - Menu của Claude Code (câu hỏi, xin quyền, `/model`…) hiện thành nút bấm.
  - Có các phím nhanh và lệnh `/` thường dùng.
  - Dán ảnh chụp màn hình vào ô nhập.
  - Nút ✨ viết lại prompt bằng Haiku.
  - Khung tệp của từng phiên và trình quản lý tệp dự án.
- **Nhiều tài khoản Claude:** đăng nhập tài khoản ngay trên web và chọn tài khoản cho từng phiên. Khi chuyển một phiên sang tài khoản khác, hội thoại được chép sang và phiên tiếp tục ở đó. Web hiện mức dùng 5 giờ / 7 ngày của từng tài khoản.
- **Kho skill dùng chung:** gắn skill vào từng dự án bằng junction, và tự thêm vào `.git/info/exclude` để không lọt vào git.
- **`ccfleet tell`:** nhắn tin giữa các phiên. Tin được gõ vào phiên nhận khi phiên đó rảnh.
- **Hook thu nhỏ ảnh:** thu nhỏ ảnh lớn trước khi Claude đọc, để tiết kiệm token.
- **Song ngữ Anh / Việt**
  - Web: tự chọn theo ngôn ngữ trình duyệt (`vi*` → tiếng Việt). Có nút chuyển EN/VI, lựa chọn được lưu trong trình duyệt.
  - CLI: đặt `"lang": "vi"` trong `config.json`, hoặc đặt biến `LANG` bắt đầu bằng `vi`. Mặc định là tiếng Anh.

## Yêu cầu

- Windows 10/11 hoặc Windows Server
- Python 3.11+ và Pillow
- Claude Code CLI đã cài và đã đăng nhập
- Gói Claude (Pro / Max) cho mỗi tài khoản

## Bắt đầu nhanh

```powershell
git clone https://github.com/<you>/ccfleet.git
cd ccfleet
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

.venv\Scripts\python ccfleet.py add my-app --cwd C:\Projects\my-app   # thêm một phiên
.venv\Scripts\python ccfleet.py run        # chạy supervisor (cửa sổ hiện tại)
.venv\Scripts\python ccfleet.py install    # hoặc: scheduled task ccfleet-supervisor, chạy lúc khởi động
.venv\Scripts\python ccfleet.py shortcut   # tạo lệnh `ccfleet` trên PATH
```

Bật giao diện web:

```powershell
ccfleet web enable    # rồi khởi động lại supervisor
ccfleet web open      # link đăng nhập dùng 1 lần (2 phút), mở trên chính máy này
ccfleet web setup     # đặt mật khẩu + TOTP để truy cập từ xa
```

ccfleet không dùng cổng cố định. Lần đầu dùng web, ccfleet chọn ngẫu nhiên một cổng trống trong khoảng 20000–60000 trên 127.0.0.1, lưu vào `web.port` trong `config.json` và in ra một lần. Gõ `ccfleet web port` để xem cổng hiện tại, hoặc `ccfleet web port N` để đổi sang cổng khác. Sau đó web chạy ở `http://127.0.0.1:<port>`.

> Cổng ngẫu nhiên **không** phải biện pháp bảo mật. Thứ bảo vệ thật sự là chỉ nghe trên loopback, có TLS phía trước, và mật khẩu + TOTP.

## Demo

```powershell
ccfleet demo --lang vi    # in ra http://127.0.0.1:<cổng ngẫu nhiên>  (--port N để chọn cổng)
```

Demo dùng dữ liệu giả, gồm vài phiên, hội thoại, tệp và hai tài khoản. Chế độ này không cần Claude, không cần đăng nhập
và không đụng tới dữ liệu thật. Demo chỉ chạy trên loopback (127.0.0.1), dùng để chụp màn hình hoặc thử giao diện.

## Bảo mật

> [!IMPORTANT]
> Mặc định các phiên chạy với `bypassPermissions`. Ai dùng được giao diện web là bảo được Claude chạy bất kỳ lệnh nào trên máy này,
> nên **truy cập web tương đương truy cập shell**.

- Web chỉ nghe trên **127.0.0.1**. **Không bao giờ mở hay port-forward thẳng cổng này ra ngoài.**
- Muốn truy cập từ xa thì dùng tunnel hoặc **reverse proxy có TLS** (xem phần Truy cập từ xa bên dưới).
- Đăng nhập bắt buộc có **mật khẩu + mã TOTP** và không thể tắt; chỉ chế độ demo chạy trên loopback là không có đăng nhập. Nhập sai nhiều lần sẽ bị khoá IP.
- Muốn đăng xuất mọi thiết bị: xoá `web-sessions.json` rồi khởi động lại supervisor.
- Cookie phiên chỉ được lưu dạng băm. Mọi request thay đổi dữ liệu phải có header `X-Fleet: 1`. Thao tác được ghi vào `web-audit.log`.
- Chế độ demo không có đăng nhập, nên từ chối chạy trên địa chỉ khác loopback.

## Truy cập từ xa (điện thoại / bên ngoài)

ccfleet luôn chỉ nghe trên loopback. Muốn vào từ điện thoại hay từ mạng khác thì cần đặt thêm một lớp có TLS phía trước:

- **Cloudflare Tunnel** (khuyên dùng): không phải mở cổng nào trên máy, và có thể thêm Cloudflare Access làm lớp đăng nhập phụ.
- **Tailscale** (`tailscale serve`): chỉ các thiết bị trong tailnet của bạn truy cập được.
- **Reverse proxy có TLS**: Caddy (tự lấy HTTPS) hoặc nginx + certbot (xem `deploy/nginx.example.conf`).

Hướng dẫn chi tiết từng bước: **[docs/remote-access.vi.md](docs/remote-access.vi.md)** (English: [docs/remote-access.md](docs/remote-access.md)).

## ccfleet Pro / Premium

Cần thêm tính năng? Bản thương mại có thêm:

- **Hub + node nhiều máy:** điều khiển phiên trên nhiều máy PC và Mac từ một giao diện web.
- **Link chia sẻ chỉ xem:** bảo vệ bằng PIN, người xem có thể góp ý.
- **MCP server:** điều khiển các phiên từ claude.ai hoặc các MCP client khác.
- **Hàng đợi task theo dự án:** task được tự động giao cho phiên.
- **Khôi phục phiên:** lấy lại phiên từ ổ cứng của máy cũ hoặc máy hỏng.

Liên hệ: <CONTACT_EMAIL>

## Giấy phép

[MIT](LICENSE)

Không liên kết với Anthropic. Claude và Claude Code là nhãn hiệu của Anthropic.
