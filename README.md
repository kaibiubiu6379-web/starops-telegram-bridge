# STAROps → Telegram webhook bridge

接受 STAROps 的自定义 Webhook（JSON、FORM 或纯文本），将完整请求内容转发到配置的 Telegram 私聊/群组。服务不落盘保存报告，也不在日志记录报告正文或密钥。

## 配置

准备以下环境变量（不要提交 `.env` 或把 Token 放进命令行/聊天记录）：

- `TELEGRAM_BOT_TOKEN`：专用 Telegram Bot Token。
- `TELEGRAM_CHAT_ID`：接收报告的用户、群组或频道 ID；群组/频道需先将 Bot 加入并授予发消息权限。
- `WEBHOOK_SHARED_SECRET`：至少 32 字节的随机值；鉴权启用时，StarOps Header 配置为 `X-Webhook-Token: <同一密钥>`。
- `WEBHOOK_AUTH_ENABLED`：默认 `true`。只有在安全组/主机防火墙已严格限制 StarOps 来源 IP 时，才可设为 `false` 关闭 Header 鉴权。
- `LOG_LEVEL`：可选，默认 `INFO`。

## 部署到 Linux 主机

将本目录上传至主机 `/data/app/starops-telegram-bridge`，然后以有 sudo 权限的账号执行：

```sh
sudo useradd --system --home /data/app/starops-telegram-bridge --shell /usr/sbin/nologin starops-bridge
sudo python3 -m venv /data/app/starops-telegram-bridge/.venv
sudo /data/app/starops-telegram-bridge/.venv/bin/pip install -r /data/app/starops-telegram-bridge/requirements.txt
sudo install -d -o starops-bridge -g starops-bridge -m 0750 /data/logs/starops-telegram-bridge
sudo install -m 600 .env.example /etc/starops-telegram-bridge.env
sudo chown -R starops-bridge:starops-bridge /data/app/starops-telegram-bridge
sudo install -m 644 starops-telegram-bridge.service /etc/systemd/system/starops-telegram-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now starops-telegram-bridge
sudo systemctl status starops-telegram-bridge --no-pager
```

先编辑 `/etc/starops-telegram-bridge.env` 填入真实值，再启动服务。服务单元默认从 `/data/app/starops-telegram-bridge` 启动。Linux 防火墙/阿里云安全组需允许 TCP/8086 入站。健康检查：`GET /healthz`。Webhook：`POST /webhook`。

应用日志以追加方式写入 `/data/logs/starops-telegram-bridge/app.log`。服务用户 `starops-bridge` 必须对日志目录有写权限。日志使用 `WatchedFileHandler`，支持外部轮转后重新打开文件。HTTP 访问记录包含客户端 IP、方法、路径、状态码、耗时和请求 Header；敏感 Header（包括 Cookie、Authorization、X-Webhook-Token）只记录名称并将值脱敏，同时记录 Webhook Token 是否存在及是否匹配服务端配置。通过鉴权的 Webhook 正文也会记录，常见 token/secret/password/credential 字段会脱敏。请按报告敏感级别保护日志文件并配置合理保留期。Gunicorn/systemd 启动层面的日志仍可通过 `journalctl -u starops-telegram-bridge` 查看。

## StarOps 配置

在“通知管理 → 自定义 Webhook”中新建对象：

- URL：`http://<主机公网地址>:8086/webhook`
- Method：`POST`
- 数据格式：`JSON`（如 StarOps 的通知正文配置为表单，再选 `FORM`）
- Header：鉴权启用时配置 `X-Webhook-Token`，值为 `WEBHOOK_SHARED_SECRET`；若已在服务端设置 `WEBHOOK_AUTH_ENABLED=false`，无需配置 Header。

然后在对应 Mission 的设置中关联这个 Webhook 通知对象。StarOps 文档说明 Webhook 有 5 秒超时和 3 次重试；本服务限制 Telegram API 等待 4 秒。小于约 3900 字符的报告直接发消息，更长的完整正文作为 JSON/TXT 文件发送；单个入站请求上限 10 MiB。

当前给出的地址使用明文 HTTP。报告可能含有资源标识、告警详情等信息；生产长期使用建议绑定域名并经 HTTPS 反向代理。关闭 Header 鉴权后，必须通过安全组和主机防火墙限制来源 IP，避免公网任意来源伪造请求。
