# Telegram to QQ Forwarder

一个轻量的 Telegram -> QQ 单向转发服务。Telegram 侧使用 Telethon Userbot 登录普通账号，NapCat 侧使用 OneBot HTTP API。项目同时提供一个可选的 Web 控制面板。

## 功能

- 首次启动时列出 Telegram 私聊和群组，交互式选择一个来源。
- 列出 NapCat 可见的 QQ 好友和 QQ 群，交互式选择一个目标。
- 保存路由到 `config.json`，后续启动自动恢复。
- 支持文本、图片和 Telegram 贴纸；静态贴纸按图片转发，动态/视频贴纸优先转发缩略图，无法取得缩略图时发送原始贴纸文件。
- 默认只监听 Telegram 收到的消息，不转发自己发出的消息。
- Web 面板支持多用户登录；管理员可以创建或停用账号。
- 每个面板用户拥有独立的 Telegram session、授权状态、对话列表和转发规则；用户之间不会共用 Telegram 账号。
- QQ/NapCat 连接是全局共用的，面板可以动态添加、停用、删除多条转发规则，来源支持 Telegram 私聊和群组，目标支持 QQ 好友和群组。
- 面板启动后会动态更新各用户的 Telegram 监听器，不需要手动编辑路由文件。
- 支持对 QQ 中的转发消息使用 OneBot reply 回复，回复内容会发回该规则所属用户的 Telegram 对话。

## 准备

1. 在 <https://my.telegram.org/apps> 创建 Telegram 应用，取得 `api_id` 和 `api_hash`。
2. 在 NapCat 中启用 OneBot HTTP API，确认桥接程序能访问其 API 地址。
3. 复制 `.env.example` 为 `.env` 并填写配置。

## 启动

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python bridge.py
```

## Web 面板

在 `.env` 中额外设置以下变量（密码至少 8 位）：

```dotenv
PANEL_SECRET=随机的长字符串
ADMIN_USERNAME=admin
ADMIN_PASSWORD=请替换为强密码
PANEL_DB=data/panel.db
TG_SESSION_DIR=data/sessions
```

后端依赖已经包含在 `requirements.txt`，启动面板：

```bash
uvicorn server:app --host 0.0.0.0 --port 8000
```

开发前端时：

```bash
cd web
npm install
npm run dev
```

Vite 开发服务器会将 `/api` 代理到 `127.0.0.1:8000`。生产部署先在 `web/` 执行 `npm run build`，再启动 Uvicorn；构建产物会由后端根路径提供，面板地址为 `http://127.0.0.1:8000/`，也可使用 `/panel/`。

每个面板用户首次进入“转发规则”页时，都需要独立完成 Telethon 普通账号登录：输入自己的手机号、验证码，若账号开启二步验证再输入密码。授权成功后会在 `TG_SESSION_DIR` 下生成 `user_<用户ID>.session`，并加载该用户自己的规则；如果没有登录，面板仍可先配置账号，但该用户的 Telegram 来源列表暂不可用。旧版单账号的 `TG_SESSION` 会自动迁移为第一个管理员的独立 session。

### 面板权限

- `admin` 可以查看所有转发规则，并在“用户后台”创建或停用成员/管理员账号。
- `user` 只能查看和管理自己创建的规则。
- 会话使用 HttpOnly 签名 Cookie；生产环境请设置固定的 `PANEL_SECRET`，并在 HTTPS 下将 `PANEL_COOKIE_SECURE=true`。

### QQ 回复 Telegram

NapCat 需要配置 OneBot HTTP 客户端，将消息事件 POST 到面板。当前 Docker 网络可使用：

```text
http://172.20.0.1:8000/api/onebot/events
```

在 NapCat WebUI 的 OneBot 网络配置中创建“HTTP客户端”：启用它，将 URL 设置为上面的地址，并填写与面板 `NAPCAT_ACCESS_TOKEN` 相同的 Token。`reportSelfMessage` 必须开启，否则登录 NapCat 的 QQ 账号自己发送的回复不会上报。保存后重启 NapCat。面板已经在 `172.20.0.1:8000` 监听供 NapCat 容器访问。转发消息成功后，在 QQ 中引用该消息回复，文本会发送到对应用户自己的 Telegram 会话。

新版 NapCat 直接编辑配置文件时，结构类似：

```json
{
  "network": {
    "httpClients": [
      {
        "enable": true,
        "name": "tg-qq-forwarder",
        "url": "http://172.20.0.1:8000/api/onebot/events",
        "reportSelfMessage": true,
        "messagePostFormat": "array",
        "token": "你的NAPCAT_TOKEN"
      }
    ]
  }
}
```

HTTP 客户端使用 `x-signature: sha1=...` 对请求体签名，面板已经支持该鉴权方式。配置修改后需要重启 NapCat 才会加载。

在 Debian/Ubuntu 上如果 `venv` 不可用，先安装对应的 `python3-venv` 包，或者使用已有的 Python 虚拟环境。

也可以使用 `Dockerfile` 构建面板镜像。Docker 部署前应使用持久化的 `data/` 目录保存每个用户的 `TG_SESSION_DIR`。容器默认以非交互方式启动 Uvicorn；用户授权在 Web 面板中完成。

每个用户的登录凭据保存在 `data/sessions/user_<用户ID>.session`，必须妥善保管。旧版命令行 `python bridge.py` 仍可用于单账号迁移或调试，但不参与 Web 面板的多用户运行时。

## 更换路由

停止程序后删除 `config.json`，重新启动即可重新选择 Telegram 来源和 QQ 目标。

## 注意

- 这是独立桥接服务，不是直接加载到 NapCat 的 JavaScript 插件；NapCat 只负责 QQ 发送端。
- 图片使用 Base64 发送，较大的图片会占用较多内存和请求体空间。
- 文件、语音、视频需要后续增加 OneBot 媒体段映射。
- 请确认自动化 Telegram 普通账号和 QQ 账号的使用符合相关平台规则。
