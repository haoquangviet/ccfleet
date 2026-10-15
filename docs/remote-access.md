# Remote access (from your phone / outside)

Tiếng Việt: [remote-access.vi.md](remote-access.vi.md)

ccfleet's web UI always listens on **127.0.0.1** only. To use it from your phone or from another network,
put something in front of it that adds TLS and forwards requests to `http://127.0.0.1:<port>`.
Run `ccfleet web port` to see your port.

> [!IMPORTANT]
> Sessions run with `bypassPermissions`, so anyone who gets past the login has a shell on this machine.
> Before going remote, set a password and TOTP with `ccfleet web setup`, and read the [security checklist](#security-checklist).

The placeholders below are `fleet.example.com` (your hostname) and `<port>` (your ccfleet web port).

---

## A) Cloudflare Tunnel (recommended)

The tunnel makes an outbound connection to Cloudflare. You open no inbound ports and don't touch your router.

1. Install `cloudflared` (for example `winget install --id Cloudflare.cloudflared`).
2. Sign in and create the tunnel:

   ```powershell
   cloudflared tunnel login
   cloudflared tunnel create ccfleet
   cloudflared tunnel route dns ccfleet fleet.example.com
   ```

3. Create `%USERPROFILE%\.cloudflared\config.yml`:

   ```yaml
   tunnel: <tunnel-id>
   credentials-file: C:\Users\<you>\.cloudflared\<tunnel-id>.json
   ingress:
     - hostname: fleet.example.com
       service: http://127.0.0.1:<port>
     - service: http_status:404
   ```

4. Run it as a Windows service:

   ```powershell
   cloudflared service install
   ```

5. Optional but recommended: add a **Cloudflare Access** (Zero Trust) application for `fleet.example.com`.
   Visitors must then pass Cloudflare's login (email one-time code, SSO…) before they even see the ccfleet login page.

**Quick try-out without a domain:**

```powershell
cloudflared tunnel --url http://127.0.0.1:<port>
```

This prints a random temporary `*.trycloudflare.com` URL. **Anyone who has that URL reaches your login page.** Use it only for a short test, and stop it afterwards.

## B) Tailscale

When your phone and this machine are both on your tailnet:

```powershell
tailscale serve --bg http://127.0.0.1:<port>
```

The UI is then reachable at `https://<machine>.<tailnet>.ts.net` from your tailnet devices only. It is not public.

## C) Reverse proxy with TLS

Use this if you already have a public hostname pointing at the machine, with ports 80 and 443 reachable.

**Caddy** obtains and renews the HTTPS certificate automatically, and sets the `X-Forwarded-*` headers itself:

```caddyfile
fleet.example.com {
    reverse_proxy 127.0.0.1:<port>
}
```

**nginx + certbot:** start from [`deploy/nginx.example.conf`](../deploy/nginx.example.conf), then:

```powershell
certbot --nginx -d fleet.example.com
```

Whatever proxy you use, it must:
- forward `Host`, `X-Forwarded-Proto`, `X-Forwarded-For` (and `X-Real-IP`);
- set `proxy_read_timeout` (or equivalent) to **≥ 120 s**, because the chat view long-polls;
- allow request bodies up to **100 MB** (`client_max_body_size 100m`) for file uploads.

## D) Running it as a Windows service

- **cloudflared:** `cloudflared service install` (see A).
- **Caddy / nginx:** wrap them with [NSSM](https://nssm.cc/), or register a scheduled task that runs at startup.
- **ccfleet** itself: `ccfleet install` registers the `ccfleet-supervisor` scheduled task.

## Behind a proxy: what ccfleet does

- It trusts `X-Forwarded-For` / `X-Real-IP` **only when the TCP peer is 127.0.0.1 or ::1**, i.e. your local proxy. The client IP from those headers is used for the login lockout and the audit log.
- It sets the **Secure** flag on its cookie when `X-Forwarded-Proto` is `https`. Plain http on loopback works without it.
- Set `web.base_url` in `config.json` (for example `"base_url": "https://fleet.example.com"`), so that `ccfleet web setup` prints a link you can open from your phone.

## Security checklist

- [ ] Never port-forward or expose the raw ccfleet port. Keep the loopback bind (`"host": "127.0.0.1"`).
- [ ] Password and TOTP are enforced; there is no way to disable them, apart from `ccfleet demo`, which only runs on loopback.
- [ ] Remember that sessions run with `bypassPermissions`: **anyone who gets in has a shell.**
- [ ] Use a strong, unique password, and keep the TOTP secret only in your authenticator app.
- [ ] Review `web-audit.log` in the data dir from time to time.
- [ ] To revoke every logged-in device, delete `web-sessions.json` and restart the supervisor.
- [ ] Consider an extra layer such as Cloudflare Access or Tailscale.
