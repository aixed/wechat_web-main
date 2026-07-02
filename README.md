# WeChat Web Client

一个基于 FastAPI + React 的微信 Web 客户端示例项目，用于连接本地或远程微信 Hook/协议服务，提供会话列表、聊天消息、媒体加载、发送消息和实时回调展示等能力。

> 注意：本项目涉及微信 Hook/协议相关能力，请仅在你拥有授权的环境中学习、测试和使用，并自行遵守相关法律法规与平台规则。

## 功能特性

- 支持 `local_hook`、`remote_hook`、`remote_protocol` 三种连接模式。
- 后端使用 FastAPI 封装微信 Hook/协议接口，并通过 WebSocket 向前端推送消息。
- 前端使用 React + TypeScript + Vite，实现类似聊天客户端的会话与消息界面。
- 支持联系人、群聊、历史消息、未读状态、头像缓存和群成员昵称解析。
- 支持文本、图片、文件发送，以及图片、表情、语音、视频等媒体消息展示辅助接口。
- 提供自动登录脚本 `backend/login_remote_hook.py`，可优先尝试免扫码登录，失败后回退到扫码登录。

## 技术栈

- 后端：Python、FastAPI、Uvicorn、httpx、WebSocket、PyYAML
- 前端：React、TypeScript、Vite、Tailwind CSS
- 通信：REST API + WebSocket

## 目录结构

```text
wechat_web/
├── backend/              # FastAPI 后端与微信接口封装
├── frontend/             # React + Vite 前端
├── config.example.yaml   # 脱敏配置模板
├── config.yaml           # 本地运行配置（不会提交到 Git）
└── README.md
```

## 快速开始

### 1. 准备配置

复制配置模板：

```bash
cp config.example.yaml config.yaml
```

然后根据你的 Hook/协议服务地址、端口、回调公网 IP 和 RDV 标识修改 `config.yaml`。

### 2. 启动后端

```bash
cd backend
pip install -r requirements.txt
python login_remote_hook.py
python main.py
```

后端默认监听 `5000` 端口，回调端口由 `config.yaml` 中的 `callback_port` 控制。

### 3. 启动前端

```bash
cd frontend
npm install
npm run dev
```

前端开发服务默认由 Vite 启动，接口通过同源代理访问后端。

## 常用命令

```bash
# 前端构建
cd frontend
npm run build

# 前端代码检查
cd frontend
npm run lint
```

## 配置说明

`config.yaml` 不会被提交到仓库。公开部署或提交代码前，请确认没有把真实 IP、RDV、Token、账号信息、日志、缓存文件或聊天媒体数据提交到 Git。

主要配置项：

- `wechat_mode`：微信使用方式，`1`=本地 Hook，`2`=远程服务器 Hook，`3`=远程服务器协议。旧配置 `login` 仍兼容，可选 `local_hook`、`remote_hook`、`remote_protocol`。
- `*_host` / `*_api_port` / `*_mgr_port`：不同模式下的服务地址和端口。
- `ip`：远程 Hook 回调可访问到的公网地址。
- `RDV`：远程服务所需的连接或鉴权标识。
- `server_port`：后端主服务端口。
- `callback_port` / `callback_path`：远程 Hook 回调地址配置。
- `recvtype`：Hook 消息接收类型，默认 `1`；`1` 为 Hook 直接返回 `msglist`，`2` 为 protobuf/raw `pb_msg`。旧配置 `recv_type` 仍兼容。
- `WECHAT_FILES_BASE`：可选环境变量，用于指定本机微信文件目录；不设置时会按 `APPDATA/WxDirDataPath/<RDV>/WeChat Files` 推导。

## 开源前注意事项

- `frontend/node_modules/`、后端缓存、上传文件、日志和本地配置均已通过 `.gitignore` 排除。
- 如果你已经误提交敏感配置，请立即撤销对应密钥或令牌，并清理 Git 历史。
- 本仓库默认不包含可运行的微信 Hook 服务本体，需要你自行准备对应服务。
