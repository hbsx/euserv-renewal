# EUserv 自动续期工具

使用 Selenium 登录 EUserv 客户控制台，在合同开放续期时执行续期；如果网站发送安全 PIN，脚本会从 Gmail IMAP 读取验证码并填回页面。可选通过 Telegram 推送成功、失败或需要人工检查的结果。

> 本工具仅适用于你本人有权操作的账户。请遵守 EUserv 服务条款。页面结构或验证机制可能随时变化，首次使用以及网站改版后应先进行非提交测试。

## 功能

- EUserv 使用 Chromium 直连，可复用固定浏览器会话。
- Gmail IMAP 可单独使用 SOCKS5 代理，不影响 EUserv 流量。
- Telegram 可单独使用 SOCKS5 代理。
- 默认不提交最终续期；只有 `--commit` 才会正式确认。
- 每次运行最多执行一次续期，不自动重复登录或重复提交。
- 正式运行时推送三类 Telegram 结果：
  - `✅` 页面明确显示续期成功；
  - `⚠️` 没有找到续期入口，可能已续期或尚未开放；
  - `❌` 登录、验证码、浏览器操作失败，或提交后未确认成功。
- 没有续期入口时保存 `no-renewal-action.png` 供排查。

## 文件说明

```text
euserv_renew.py                 主程序
.env.example                    配置模板，不含密钥
requirements.txt               pip 依赖
deploy/euserv-renewal.service   systemd 服务
deploy/euserv-renewal.timer     每月定时器示例
```

## Debian 安装

以下命令以 Debian 11/12、root 用户和安装目录 `/opt/euserv-renewal` 为例。

### 1. 安装系统依赖

```bash
apt update
apt install -y git python3 python3-selenium python3-socks chromium chromium-driver util-linux
```

如果发行版没有 `python3-selenium`，可以改用虚拟环境：

```bash
apt install -y python3-venv chromium chromium-driver util-linux
python3 -m venv /opt/euserv-renewal/.venv
/opt/euserv-renewal/.venv/bin/pip install -r /opt/euserv-renewal/requirements.txt
```

使用虚拟环境时，需要把 systemd 服务中的 `/usr/bin/python3` 改成 `/opt/euserv-renewal/.venv/bin/python`。

### 2. 下载项目

```bash
git clone https://github.com/hbsx/euserv-renewal.git /opt/euserv-renewal
cd /opt/euserv-renewal
cp .env.example .env
chmod 600 .env
```

### 3. 填写配置

```bash
nano /opt/euserv-renewal/.env
```

至少填写：

```ini
EUSERV_USERNAME=你的EUserv邮箱或客户ID
EUSERV_PASSWORD=你的EUserv密码
EUSERV_CONTRACT_NUMBER=你的合同编号

GMAIL_ADDRESS=your-address@gmail.com
GMAIL_APP_PASSWORD=Google生成的16位应用专用密码
```

Gmail 应用专用密码需要 Google 账号已开启两步验证。不要使用 Gmail 登录密码，也不要把 `.env` 上传到 GitHub。

如果只有 Gmail 需要 SOCKS5 代理：

```ini
GMAIL_SOCKS_HOST=<SOCKS代理地址>
GMAIL_SOCKS_PORT=7890
GMAIL_IMAP_TIMEOUT=20
```

Chromium/EUserv 不会读取这些 Gmail 代理设置。不要在脚本外层使用 `proxychains`，否则 EUserv 也会经过代理。

### 4. 配置 Telegram（可选）

1. 在 Telegram 联系官方 `@BotFather`，使用 `/newbot` 创建机器人。
2. 向新机器人发送一次 `/start`。
3. 使用 Bot API 的 `getUpdates` 获取消息中的 `chat.id`。
4. 在 `.env` 填写：

```ini
TELEGRAM_BOT_TOKEN=BotFather生成的Token
TELEGRAM_CHAT_ID=接收通知的Chat ID
```

如果 Telegram 也需要 SOCKS5 代理：

```ini
TELEGRAM_SOCKS_HOST=<SOCKS代理地址>
TELEGRAM_SOCKS_PORT=7890
```

测试通知：

```bash
cd /opt/euserv-renewal
python3 -c "import euserv_renew as app; app.load_dotenv(); app.notify_telegram('✅ EUserv 通知测试')"
```

## 手动测试

先检查语法和浏览器：

```bash
cd /opt/euserv-renewal
python3 -m py_compile euserv_renew.py
python3 -c "from selenium import webdriver; o=webdriver.ChromeOptions(); o.add_argument('--headless=new'); o.add_argument('--no-sandbox'); o.add_argument('--disable-dev-shm-usage'); o.binary_location='/usr/bin/chromium'; d=webdriver.Chrome(options=o); print('Chromium OK'); d.quit()"
```

非提交测试：

```bash
python3 -u euserv_renew.py
```

正式执行：

```bash
python3 -u euserv_renew.py --commit
```

`--commit` 会允许最终续期提交。首次测试不要添加它。若页面出现图形验证码，脚本不会绕过验证码；无头环境下本次运行会失败并退出。

## 每月自动运行

仓库内的定时器示例设为每月 25 日、系统本地时间 07:30 运行。先确认时区：

```bash
timedatectl
```

安装服务：

```bash
install -m 644 deploy/euserv-renewal.service /etc/systemd/system/euserv-renewal.service
install -m 644 deploy/euserv-renewal.timer /etc/systemd/system/euserv-renewal.timer
systemctl daemon-reload
systemctl enable --now euserv-renewal.timer
systemctl list-timers euserv-renewal.timer --no-pager
```

服务带有 `--commit`，因此定时触发属于正式运行。`flock` 防止并发；服务不自动重启；`Persistent=false` 表示错过当月时间后不会补跑。

查看日志：

```bash
journalctl -u euserv-renewal.service -n 100 --no-pager
```

停止自动运行：

```bash
systemctl disable --now euserv-renewal.timer
```

修改运行日期或时间时，编辑 `deploy/euserv-renewal.timer` 中的 `OnCalendar`，重新复制到 `/etc/systemd/system/` 并执行 `systemctl daemon-reload` 和 `systemctl restart euserv-renewal.timer`。

## 更新

```bash
cd /opt/euserv-renewal
git pull --ff-only
python3 -m py_compile euserv_renew.py
```

更新脚本不需要重新创建定时器；下一次触发会直接使用新版本。

## 安全建议

- `.env` 必须保持 `chmod 600`，绝不能提交到版本库。
- 不要把终端中的 Token、密码、完整验证码或调试页面源码公开。
- 固定使用一个 EUserv 出口 IP 和一个浏览器资料目录，避免短时间反复登录。
- 不要在多台机器同时运行同一账户的自动续期。
- 续期结果应以 EUserv 控制台或官方确认邮件为准。

## 常见问题

### Gmail TLS 能连接，但认证超时

设置 `GMAIL_SOCKS_HOST` 和 `GMAIL_SOCKS_PORT`，并确认已安装 `python3-socks`。代理只用于 Gmail IMAP。

### 显示 `No renewal action is currently available`

可能是合同已续期、尚未进入续期窗口，或 EUserv 页面已改版。检查 `no-renewal-action.png` 和 systemd 日志，并登录控制台核对。

### 定时器没有日志

在首次触发前，`journalctl` 显示 `No entries` 属于正常现象。使用 `systemctl list-timers euserv-renewal.timer --no-pager` 查看下一次运行时间。

### 图形验证码

本工具不会规避验证码。需要在有图形界面的可信设备上人工完成验证，或者直接手动续期。
