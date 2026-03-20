# sockpuppetbrowser-cloakbrowser

A high-performance WebSocket proxy for the Chrome DevTools Protocol (CDP), backed by **[CloakBrowser](https://github.com/CloakHQ/CloakBrowser)** — a patched Chromium distribution with C++-level fingerprint spoofing for canvas, WebGL, audio, navigator signals, and automation detection.

This project is a derivative of **[sockpuppetbrowser](https://github.com/dgtlmoon/sockpuppetbrowser)** and serves as the browser back-end for the [changedetection.io](https://changedetection.io/) project when anti-detection browser capabilities are needed.

---

## What is this?

Connect to `ws://127.0.0.1:3000` as your CDP browser URL and the proxy will spin up a fresh, isolated CloakBrowser instance for that request. When the connection closes the browser is killed and cleaned up automatically.

This is **not a fork of CloakBrowser** — it is a CDP proxy wrapper that installs and uses the `cloakbrowser` pip package. All browser fingerprinting capabilities come directly from CloakBrowser's patched Chromium binary.

---

## Anti-detection features (provided by CloakBrowser)

- **33 C++ patches to Chromium source** — canvas, WebGL, audio context, navigator, plugins, screen geometry, hardware concurrency, GPU/renderer strings
- **`navigator.webdriver = false`** — automation flag suppressed
- **`HeadlessChrome` removed from User-Agent** — headless mode undetectable
- **Deterministic or random fingerprints** — set `FINGERPRINT_SEED` for reproducibility or leave blank for a random seed per session
- **Platform spoofing** — `FINGERPRINT_PLATFORM=windows|macos|linux`
- **WebRTC IP leak prevention** — disabled by default (`BLOCK_WEBRTC=true`)
- **CDP automation signal suppression** — via `patchright` backend (enabled by default)

---

## Quick start

```bash
docker compose up
```

Connect Playwright, Puppeteer, or any CDP client to `ws://localhost:3000`.

---

## Configuration (docker-compose.yml environment variables)

| Variable | Default | Description |
|---|---|---|
| `MAX_CONCURRENT_CHROME_PROCESSES` | `10` | Maximum simultaneous browser instances |
| `MIN_AVAILABLE_MEMORY_MB` | `500` | Reject new connections below this free RAM threshold |
| `DROP_EXCESS_CONNECTIONS` | `False` | `True` = queue excess connections; `False` = reject immediately |
| `FINGERPRINT_SEED` | _(empty)_ | Integer seed for deterministic fingerprint; empty = random per session |
| `FINGERPRINT_PLATFORM` | `windows` | Spoofed OS platform: `windows`, `macos`, or `linux` |
| `EXTRA_FINGERPRINT_ARGS` | _(empty)_ | Additional `--fingerprint-*` flags passed to the binary |
| `BLOCK_WEBRTC` | `true` | Disable the WebRTC stack entirely to prevent IP leaks |
| `WEBRTC_IP_HANDLING_POLICY` | `disable_non_proxied_udp` | WebRTC policy used when `BLOCK_WEBRTC=false` |
| `CLOAKBROWSER_BACKEND` | `patchright` | `patchright` for extra CDP suppression; `playwright` for standard |
| `CHROME_HEADFUL` | `false` | Run with a virtual display (Xvfb) instead of headless |
| `SCREEN_WIDTH` / `SCREEN_HEIGHT` | _(unset)_ | Browser viewport size |
| `LOG_LEVEL` | `DEBUG` | `TRACE`, `DEBUG`, `INFO`, `SUCCESS`, `WARNING`, `ERROR`, `CRITICAL` |
| `ALLOW_CDP_LOG` | `no` | Set to `yes` to enable CDP protocol file logging (use `?log-cdp=/path` in URL) |
| `STARTUP_DELAY` | `0` | Seconds to wait before accepting connections |

### Controlling the Chromium version

Each `cloakbrowser` pip release ships a specific patched Chromium build. To pin a version, set the build argument in `docker-compose.yml`:

```yaml
build:
  args:
    CLOAKBROWSER_PIP_VERSION: "0.3.18"
```

Leave blank to use the latest available version.

---

## Licenses

This project contains components under multiple licenses.

### This project (sockpuppetbrowser-cloakbrowser)

Apache License 2.0. Derived from [sockpuppetbrowser](https://github.com/dgtlmoon/sockpuppetbrowser).

### CloakBrowser wrapper code

MIT License — Copyright (c) 2026 CloakHQ.

Permission is granted to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the software, provided the copyright notice is included.

### CloakBrowser patched Chromium binary

**Proprietary — CloakBrowser Binary License v1.0** (February 2026) — Copyright (c) 2026 CloakHQ. All rights reserved.

Key terms (see [BINARY-LICENSE.md](https://github.com/CloakHQ/CloakBrowser/blob/main/BINARY-LICENSE.md) for full text):

- **Use**: Non-exclusive, royalty-free license for personal or commercial use at no cost.
- **Docker/container use**: Permitted. Internal storage and execution within organisational infrastructure is explicitly allowed.
- **Redistribution**: **Not permitted.** The binary must be obtained from GitHub Releases or `cloakbrowser.dev`. You may not redistribute, resell, or bundle the binary for third-party distribution without a separate OEM/SaaS licence (contact `cloakhq@pm.me`).
- **No reverse engineering**: You may not reverse-engineer, decompile, or modify the binary.
- **Indemnification**: Users bear sole responsibility for their use. You must indemnify CloakHQ from any claims arising from unlawful use or licence violations.
- **Prohibited uses**: Unauthorized system access, credential stuffing, circumventing authentication, fraud, identity theft.
- **Warranty**: Provided "AS IS" without any warranties.
- **Liability cap**: Maximum aggregate liability is $100 USD.
- **Telemetry**: No intentional telemetry; built on ungoogled-chromium.

### Chromium

The patched Chromium binary is based on the [Chromium](https://www.chromium.org/) project, which is licensed under the [BSD 3-Clause License](https://chromium.googlesource.com/chromium/src/+/main/LICENSE) and incorporates numerous third-party components with their own licenses. See the Chromium project for full details.

---

## Disclaimer

This project is licensed under the Apache License 2.0, which already provides a full warranty disclaimer and limitation of liability (Sections 7 and 8). The additional terms below are not duplicated by that license.

### User Responsibility and Indemnification

**You are solely responsible for ensuring that your use of this software complies with all applicable laws, regulations, and the terms of service of any website, platform, or service you interact with.**

By using this software you agree to **indemnify, defend, and hold harmless** the authors, contributors, and copyright holders of this project from and against any and all claims, damages, losses, liabilities, costs, and expenses (including reasonable attorneys' fees) arising out of or related to your use or misuse of this software, your violation of any applicable law or regulation, your violation of any third-party rights or terms of service, or any breach of the CloakBrowser Binary License terms resulting from your use.

Note: CloakBrowser's Binary License independently requires you to indemnify CloakHQ under the same circumstances. Both obligations apply when you use this project.

### Prohibited Uses

The authors do not condone and expressly prohibit the use of this software for:

- Unauthorised access to computer systems, networks, or accounts
- Credential stuffing, password spraying, or brute-force attacks
- Circumventing authentication mechanisms or security controls
- Scraping or harvesting data in violation of a site's terms of service
- Abusive account creation, sockpuppeting, or astroturfing
- Ad fraud, click fraud, or automated manipulation of metrics
- Fraud, identity theft, impersonation, or phishing
- Any activity that violates applicable law or the rights of third parties
