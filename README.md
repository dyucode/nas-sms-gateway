# NAS SMS Gateway

**Receive SMS on a home NAS with a USB 4G module, and get the full text on your phone via Synology Chat or Telegram. Receive-only, self-hosted, no cloud.**

[English](#english) · [中文](#中文)

`sms-gateway` · `4g-modem` · `usb-modem` · `synology` · `docker` · `telegram-bot` · `verification-codes` · `china-sim` · `dji`

---

## English

### Why

Many services (banks, apps, government portals, some Chinese platforms) only send verification codes by SMS
to a number in one specific country. If you live abroad, keeping that SIM alive and reading its messages
remotely is awkward and often expensive (roaming, forwarding services, or carrying a second phone).

This project turns a cheap SIM plus a **DJI first-generation 4G USB module** into an always-on SMS receiver
that lives on a NAS at home. Incoming messages are read straight from the module over USB, stored locally,
and forwarded as **full text** to your phone.

```
 SIM + USB 4G module ──USB──▶ container on your NAS ──▶ Synology Chat ─┐
                                   │ SQLite (14 days)                   ├──▶ your phone
                                   └───────────────────▶ Telegram bot ──┘
```

### Highlights

- **Receive-only by design.** It cannot send SMS, place calls, switch eSIM profiles or delete anything.
  Only a fixed, read-only set of AT commands is allowed (see `smsgw/atguard.py`), enforced before any byte is written to the modem.
- **Full text, not a snippet.** Long (concatenated) SMS are reassembled; GSM-7 and UCS-2 are decoded; duplicates are ignored.
- **Reliable delivery.** Each channel (Chat, Telegram) has its own outbox with exponential-backoff retries, so one channel being down never blocks or duplicates the other.
- **Self-hosted and private.** Messages stay on your NAS for 14 days (configurable), are never written to logs, and secrets are only read from Docker secret files.
- **Hardened container.** Read-only filesystem, all capabilities dropped, `no-new-privileges`, no published ports (outbound only).
- **Health alerts.** If the modem or SIM is unavailable for more than 5 minutes you get a notice, and another when it recovers.
- **Tested.** 39 automated tests with a fake modem and fake Chat/Telegram servers; no hardware needed to run them.

### Compatibility

| | |
|---|---|
| Module | DJI first-generation 4G USB module (Quectel EG25-G based). USB IDs default to that module; override with `USB_VID` / `USB_PID`. |
| Host | Anything that runs Docker and exposes `/dev/bus/usb`. Tested on a Synology DS218+ with DSM 7.2. |
| Notifications | Synology Chat (incoming webhook), Telegram bot (optional) |

### Quick start

1. **Chat:** create a channel, then *Integration → Incoming Webhook*, and copy the URL (it contains a secret token).
2. Copy this folder to the NAS, e.g. `/volume1/docker/nas-sms-gateway`.
3. Create the secrets (`chmod 600`, root-owned):
   - `secrets/chat_webhook_url.txt`: the webhook URL, one line. Prefer `http://<NAS-LAN-IP>:5000/...` to avoid TLS issues.
   - `secrets/telegram.txt` *(optional)*: line 1 = bot token (from @BotFather), line 2 = your chat id. Leave empty to disable Telegram.
4. Stop any vendor app that talks to the dongle (some delete SMS from the modem after caching them), then plug in the module.
5. Start: `docker compose up -d --build`. Check `docker logs smsgw`; a "gateway started" notice should appear in Chat/Telegram. Send a test SMS.

Finding your Telegram chat id: send any message to your bot, then run
`docker exec smsgw python -m smsgw --telegram-chat-id` (a token-only `telegram.txt` is enough).
List USB interfaces without touching the modem: `docker compose run --rm smsgw python -m smsgw --list-usb`.

For https webhooks with a self-signed certificate set `CHAT_CA_FILE`, or `CHAT_INSECURE_TLS=1` (allowed only for private IPs).
Set your timezone with `TZ=Europe/Berlin docker compose up -d`.

### Security notes

- Do not expose DSM or Chat to the internet for this; reach it over a VPN (e.g. Tailscale). The container only makes outbound calls.
- Telegram bot messages are not end-to-end encrypted; SMS verification codes will transit Telegram's servers. Skip Telegram if that is not acceptable.
- Keep the bot token and webhook URL secret; rotate them (`/revoke` in BotFather, regenerate the webhook) if they leak.
- Never commit `secrets/` or `data/`; both are git-ignored.

### Development

```
pip install pytest pyusb
python -m pytest
```

### Acknowledgments

This project builds on prior community work on the DJI first-generation 4G module:

- **[DJOneHub](https://github.com/ZenGeekLabs/DJOneHub)** by ZenGeekLabs, an unofficial macOS management tool for the module
  (SMS, eSIM, AT debugging, USB internet sharing). The early groundwork here, i.e. how the module is reached over USB and
  which interface speaks AT commands, comes from studying DJOneHub.
- **[VoHive](https://github.com/iniwex5/vohive)** by iniwex5, the project DJOneHub itself is built on.

This is an independent implementation, written from scratch in Python (DJOneHub is written in Go), with a different goal
(an always-on, receive-only gateway on a NAS). **No source code from DJOneHub or VoHive is included**; only publicly
documented facts about the module (USB IDs, that SMS is read over AT commands) were learned from them. That is why this
project is MIT-licensed while DJOneHub is under the PolyForm Noncommercial License.
Not affiliated with or endorsed by those projects, DJI, Synology or Telegram. Please go star them.

### Disclaimer

Use only with SIMs and accounts you own, and follow the laws and carrier terms where you live.
Provided as is, without warranty; see `LICENSE`.

### License

MIT, see `LICENSE`.

---

## 中文

### 这是什么

很多服务(银行、App、政务、部分国内平台)只能给某个国家的号码发短信验证码。人在海外时,
保留那张 SIM 并远程收短信既麻烦又贵(漫游、转接服务、或者多带一部手机)。

本项目把一张便宜的 SIM 和 **大疆(DJI)一代 4G USB 模块** 变成放在家里 NAS 上的常驻短信接收器:
直接通过 USB 读取短信,本地保存,并把**完整短信内容**转发到你的手机。

### 特点

- **只收不发**:不能发短信、打电话、切换 eSIM,也不会删除任何东西。只允许固定的只读 AT 指令(见 `smsgw/atguard.py`),在写入模块之前就会拦截。
- **完整内容**:长短信自动拼接,支持 GSM-7 和 UCS-2 解码,自动去重。
- **可靠投递**:Chat 和 Telegram 各有独立的发送队列和指数退避重试,一边故障不会阻塞或重复另一边。
- **私有自托管**:短信只保存在你的 NAS 上(默认 14 天),不写日志;密钥只从 Docker secret 文件读取。
- **加固容器**:只读文件系统、丢弃所有 capability、`no-new-privileges`、不开放任何端口(只主动对外连接)。
- **健康告警**:模块或 SIM 异常超过 5 分钟会通知你,恢复后再通知一次。
- **有测试**:39 个自动化测试,用模拟的调制解调器和模拟的 Chat/Telegram 服务器,无需硬件即可运行。

### 快速开始

1. 在 Synology Chat 建一个频道,*集成 → 传入 Webhook*,复制 URL(含密钥 token)。
2. 把本目录复制到 NAS,例如 `/volume1/docker/nas-sms-gateway`。
3. 创建密钥文件(root 所有,`chmod 600`):
   - `secrets/chat_webhook_url.txt`:Webhook URL,一行。建议用 `http://<NAS局域网IP>:5000/...`,避开证书问题。
   - `secrets/telegram.txt`(可选):第一行是机器人 token(来自 @BotFather),第二行是你的 chat id;留空则不启用 Telegram。
4. 先关掉任何会访问这个模块的厂商软件(有的会在缓存后把短信从模块里删掉),再插上模块。
5. 启动:`docker compose up -d --build`,查看 `docker logs smsgw`,Chat/Telegram 里应出现"短信网关已启动"。发一条测试短信。

### 安全提示

- 不要为此把 DSM 或 Chat 暴露到公网,用 VPN(如 Tailscale)访问即可;容器只会主动向外连接。
- Telegram 机器人消息没有端到端加密,短信验证码会经过 Telegram 服务器。介意的话不要启用 Telegram。
- 机器人 token 和 Webhook URL 要保密,泄露后立即重置(BotFather 的 `/revoke`、重新生成 Webhook)。
- 不要提交 `secrets/` 和 `data/`,它们已被 `.gitignore` 忽略。

### 致谢与声明

感谢社区对大疆一代 4G 模块的研究,特别是 **DJOneHub**(ZenGeekLabs)——模块如何通过 USB 访问、
哪个接口处理 AT 指令,都参考了它的工作;以及 DJOneHub 所基于的 **VoHive**(iniwex5)。
本项目是用 Python 从零编写的独立实现(DJOneHub 是 Go 写的),**不包含 DJOneHub 或 VoHive 的任何源代码**,
只参考了关于模块的公开事实(USB ID、通过 AT 指令读短信)。因此本项目采用 MIT 许可证。
本项目与上述项目、DJI、Synology、Telegram 均无隶属关系。
请仅用于你自己的 SIM 和账号,并遵守当地法律与运营商条款。
