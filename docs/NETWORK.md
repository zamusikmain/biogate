# Running BioGate for other computers on the local network

By default BioGate listens only on `127.0.0.1` and the session cookie is not marked
`Secure`. That is the right setup for one PC: the browser on the same machine opens
`http://127.0.0.1:8000/` and may use the camera, because browsers treat `localhost`
as a secure context.

A terminal on **another** computer (an entrance kiosk, a reception PC) is different.
Browsers only give camera access to pages served over HTTPS (or from `localhost`), so
plain `http://192.168.1.20:8000/` loads but face login and enrollment cannot start.
This guide shows the supported way to serve BioGate over HTTPS on a local network.

It is meant for a closed LAN. Do not expose BioGate to the internet.

## Overview

```
terminal browser ──HTTPS──▶ reverse proxy (TLS, port 443) ──HTTP──▶ uvicorn 127.0.0.1:8000
```

- uvicorn keeps listening on `127.0.0.1` only; nothing but the proxy can reach it.
- The proxy terminates TLS and forwards the real client address in `X-Forwarded-For`.
- uvicorn trusts that header **only from the proxy on 127.0.0.1**, so rate limits and
  access events see each terminal's own address. Without this, every request looks
  like it came from `127.0.0.1` and one terminal's failed logins lock out all others.

## 1. Session cookie over HTTPS only

`run.ps1 -BehindProxy` (step 2) sets `BIOGATE_ADMIN_COOKIE_SECURE=true` for the server
process, overriding `.env`, so the session cookie is sent only over HTTPS. Starting
uvicorn by hand behind the proxy, put `BIOGATE_ADMIN_COOKIE_SECURE=true` in `.env`.
With this setting the console no longer works over plain `http://127.0.0.1:8000/`;
use the HTTPS address on the server PC as well.

## 2. Start BioGate behind the proxy

```powershell
powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -BehindProxy
```

which runs

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

Run a single worker (no `--workers N`): live liveness challenges are kept in process
memory and are not shared between processes.

## 3. Reverse proxy with TLS (Caddy example)

[Caddy](https://caddyserver.com/) is a single executable that creates and renews a
local certificate on its own. Pick a name for the server, for example `biogate.lan`,
and make the terminals resolve it to the server's address (a DNS record on the router,
or a line `192.168.1.20 biogate.lan` in `C:\Windows\System32\drivers\etc\hosts` on each
terminal).

`Caddyfile` next to `caddy.exe`:

```
biogate.lan {
    tls internal
    reverse_proxy 127.0.0.1:8000
}
```

Start it with `caddy run`. Caddy adds `X-Forwarded-For` and streams the live monitor
(server-sent events) without extra settings.

`tls internal` signs the certificate with Caddy's own local root CA. Each terminal must
trust that root once, otherwise the browser shows a certificate warning and may still
block the camera:

1. On the server, find the root certificate: `%AppData%\Caddy\pki\authorities\local\root.crt`.
2. Copy `root.crt` to the terminal and import it into **Trusted Root Certification
   Authorities** (double-click, *Install Certificate*, *Local Machine*), or deploy it by
   group policy.

If the organisation already has an internal CA, use a certificate from it instead of
`tls internal` (`tls C:\certs\biogate.crt C:\certs\biogate.key`).

Any other reverse proxy works the same way (IIS with ARR, nginx): terminate TLS, forward
to `127.0.0.1:8000`, set `X-Forwarded-For`, and do not buffer `text/event-stream`
responses.

## 4. Windows firewall on the server

Open only the HTTPS port, only for the local subnet, and leave port 8000 closed:

```powershell
New-NetFirewallRule -DisplayName "BioGate HTTPS" -Direction Inbound -Protocol TCP -LocalPort 443 `
    -Action Allow -Profile Private,Domain -RemoteAddress LocalSubnet
```

The network profile of the server's adapter must be *Private* or *Domain*, not *Public*.

## 5. Check from a terminal

1. Open `https://biogate.lan/` and confirm the padlock shows a valid certificate.
2. Sign in with a password, then try face login: the camera prompt must appear.
3. In the admin console, open *Access* and check that the event shows the terminal's
   address, not `127.0.0.1`.

## What stays on the server

The SQLite database, evidence photos, archives, backups and logs remain under `data/`
on the server PC. Terminals only run the browser; nothing is stored on them apart from
the session cookie and the language/theme choice.
