# Truy cập từ xa (điện thoại / bên ngoài)

English: [remote-access.md](remote-access.md)

Giao diện web của ccfleet luôn chỉ nghe trên **127.0.0.1**. Muốn dùng từ điện thoại hay từ mạng khác thì cần đặt
thêm một lớp có TLS phía trước, chuyển tiếp request vào `http://127.0.0.1:<port>`. Gõ `ccfleet web port` để xem cổng của bạn.

> [!IMPORTANT]
> Các phiên chạy với `bypassPermissions`, nên ai vượt qua được màn hình đăng nhập là có shell trên máy này.
> Trước khi mở ra ngoài, chạy `ccfleet web setup` để đặt mật khẩu + TOTP, và đọc [danh sách kiểm tra bảo mật](#danh-sách-kiểm-tra-bảo-mật).

Trong hướng dẫn, `fleet.example.com` là tên miền của bạn, còn `<port>` là cổng web của ccfleet.

---

## A) Cloudflare Tunnel (khuyên dùng)

Tunnel tự kết nối ra Cloudflare, nên không phải mở cổng vào nào và không cần cấu hình router.

1. Cài `cloudflared`, ví dụ `winget install --id Cloudflare.cloudflared`.
2. Đăng nhập và tạo tunnel:

   ```powershell
   cloudflared tunnel login
   cloudflared tunnel create ccfleet
   cloudflared tunnel route dns ccfleet fleet.example.com
   ```

3. Tạo file `%USERPROFILE%\.cloudflared\config.yml`:

   ```yaml
   tunnel: <tunnel-id>
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: fleet.example.com
       service: http://127.0.0.1:<port>
     - service: http_status:404
   ```

4. Cài thành Windows service:

   ```powershell
   cloudflared service install
   ```

5. Tuỳ chọn, nhưng nên làm: thêm một ứng dụng **Cloudflare Access** (Zero Trust) cho `fleet.example.com`. Khi đó người
   truy cập phải qua bước đăng nhập của Cloudflare (mã gửi qua email, SSO…) trước khi thấy trang đăng nhập của ccfleet.

**Thử nhanh khi chưa có tên miền:**

```powershell
cloudflared tunnel --url http://127.0.0.1:<port>
```

Lệnh này in ra một URL tạm, ngẫu nhiên, dạng `*.trycloudflare.com`. **Ai có URL đó đều vào được trang đăng nhập.** Chỉ dùng để thử trong thời gian ngắn, xong thì tắt.

## B) Tailscale

Điện thoại và máy này cùng ở trong tailnet thì chạy:

```powershell
tailscale serve --bg http://127.0.0.1:<port>
```

Sau đó mở `https://<máy>.<tailnet>.ts.net`. Chỉ các thiết bị trong tailnet của bạn truy cập được, địa chỉ này không công khai.

## C) Reverse proxy có TLS

Cách này dành cho khi đã có tên miền công khai trỏ về máy và các cổng 80/443 vào được từ ngoài.

**Caddy** tự lấy và gia hạn chứng chỉ HTTPS, và tự đặt các header `X-Forwarded-*`:

```caddyfile
fleet.example.com {
    reverse_proxy 127.0.0.1:<port>
}
```

**nginx + certbot:** bắt đầu từ [`deploy/nginx.example.conf`](../deploy/nginx.example.conf), rồi chạy:

```powershell
certbot --nginx -d fleet.example.com
```

Dùng proxy nào cũng phải:
- chuyển tiếp `Host`, `X-Forwarded-Proto`, `X-Forwarded-For` (và `X-Real-IP`);
- đặt `proxy_read_timeout` (hoặc tương đương) **≥ 120 giây**, vì màn hình hội thoại dùng long-poll;
- cho phép body tới **100 MB** (`client_max_body_size 100m`) để tải tệp lên.

## D) Chạy thành Windows service

- **cloudflared:** `cloudflared service install` (xem mục A).
- **Caddy / nginx:** bọc bằng [NSSM](https://nssm.cc/), hoặc tạo scheduled task chạy lúc khởi động máy.
- **ccfleet:** `ccfleet install` tạo scheduled task `ccfleet-supervisor`.

## ccfleet làm gì khi đứng sau proxy

- Chỉ tin `X-Forwarded-For` / `X-Real-IP` **khi kết nối TCP đến từ 127.0.0.1 hoặc ::1**, tức là từ proxy trên chính máy này. IP lấy từ các header đó được dùng để khoá đăng nhập sai và ghi nhật ký.
- Gắn cờ **Secure** cho cookie khi `X-Forwarded-Proto` là `https`. Truy cập http thường qua loopback vẫn dùng được, không cần cờ này.
- Đặt `web.base_url` trong `config.json` (ví dụ `"base_url": "https://fleet.example.com"`) để `ccfleet web setup` in ra link mở được từ điện thoại.

## Danh sách kiểm tra bảo mật

- [ ] Không bao giờ port-forward hay mở thẳng cổng của ccfleet. Giữ `"host": "127.0.0.1"`.
- [ ] Mật khẩu + TOTP luôn bắt buộc và không thể tắt, trừ `ccfleet demo` vốn chỉ chạy trên loopback.
- [ ] Nhớ rằng các phiên chạy với `bypassPermissions`: **ai vào được là có shell.**
- [ ] Dùng mật khẩu mạnh, không trùng với nơi khác; chỉ lưu khoá TOTP trong app xác thực.
- [ ] Thỉnh thoảng xem `web-audit.log` trong thư mục dữ liệu.
- [ ] Muốn thu hồi mọi thiết bị đã đăng nhập: xoá `web-sessions.json` rồi khởi động lại supervisor.
- [ ] Cân nhắc thêm một lớp bảo vệ nữa như Cloudflare Access hoặc Tailscale.
