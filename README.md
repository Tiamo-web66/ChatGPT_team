# ChatGPT_team

ChatGPT 团队账号批量注册 & Codex OAuth Refresh Token 提取工具。

## 安装

```bash
python -m pip install --upgrade pip
python -m pip install curl_cffi

# Web 模式额外依赖
pip install fastapi uvicorn
```

## 快速开始

### CLI 模式

```bash
# 注册 1 个账号
python ChatGPT_team.py --total 1 --workers 1

# 注册 10 个账号，5 线程并发
python ChatGPT_team.py --total 10 --workers 5

# 检查已有 token 套餐类型（401 自动刷新）
python ChatGPT_team.py --check-tokens

# 指定代理
python ChatGPT_team.py -p "http://127.0.0.1:7890" --total 1 --workers 1
```

## 命令行参数

| 参数 | 简写 | 说明 | 默认值 |
|------|------|------|--------|
| `--total` | `-n` | 注册账号数量 | 1 |
| `--workers` | `-w` | 并发线程数 | 1 |
| `--proxy` | `-p` | 代理地址（覆盖配置文件） | 无 |
| `--output` | `-o` | 成功输出文件 | `registered_only.txt` |
| `--check-tokens` | - | 检查已有 token 套餐类型 | - |

## Web 模式

启动 Web 管理界面，支持实时进度监控、Token 管理和配置编辑：

```bash
python web_server.py
# 浏览器打开 http://127.0.0.1:8000
```

### 功能页面

| 页面 | 说明 |
|------|------|
| 📊 仪表盘 | 实时进度条 + WebSocket 日志流，支持 暂停/搜索/清空 |
| 🔑 Token 管理 | 搜索/查看/删除/导出 token 文件 |
| ⚙️ 配置 | 表单编辑 + JSON 实时预览，一键保存 |

### 架构

```
浏览器 ←→ web_server.py (FastAPI + WebSocket) ←→ ChatGPT_team.py (核心引擎)
```

- 核心脚本零侵入：`ChatGPT_team.py` 保持单文件可独立运行，Web 层通过回调钩子获取日志和进度
- 无前端框架：单 HTML 文件，零构建步骤
- 仅监听 `127.0.0.1:8000`，本地访问

## 配置文件

脚本启动时自动查找以下文件（按优先级）：

1. `ChatGPT_team.config.local.json`
2. `ChatGPT_team.config.json`

---

### 配置项说明

```json
{
  // ========== 代理配置 ==========

  // 代理类型：static（直接使用代理地址）/ dynamic（从 API 动态获取）
  //   - static:  填写 proxy 字段，脚本直接用这个代理
  //   - dynamic: 填写 proxy_api_url 字段，脚本每次调用 API 获取真实代理
  "proxy_type": "static",

  // 【static 模式】代理地址，支持以下格式：
  //   http://host:port
  //   http://user:pass@host:port
  //   host:port:user:pass
  "proxy": "http://127.0.0.1:7890",

  // 【dynamic 模式】代理提取 API 地址
  //   要求 API 返回纯文本 ip:port（如 "156.59.223.186:19001"）
  //   如需认证参数，直接拼在 URL 里
  "proxy_api_url": "https://white.1024proxy.com/white/api?region=JP&num=1&time=10&format=1&type=txt",

  // 【dynamic 模式】代理 API 调用失败重试次数
  //   每次重试间隔递增（1s / 2s / 3s ...）
  "proxy_api_retry": 3,

  // ========== 链式代理配置（高级） ==========

  // 是否启用链式代理（本地转发 → 主代理 → 上游代理）
  "proxy_chain_enabled": false,

  // 上游代理模板，{session} 会被替换为随机字符串
  // 格式：HOST:PORT:USERNAME-region-JP-sid-{session}-t-5:PASSWORD
  "proxy_chain_upstream_proxy": "HOST:PORT:USERNAME-region-JP-sid-{session}-t-5:PASSWORD",

  // 是否每个账号生成独立粘性 session（替换 {session} 占位符）
  "proxy_chain_dynamic_per_account": false,

  // 链式代理区域
  "proxy_chain_register_region": "JP",
  "proxy_chain_payment_region": "JP",

  // ========== Sub2API 自动上传配置 ==========

  // 是否开启注册成功后自动上传到 Sub2API 中转站
  //   true  → 注册成功后将 refresh_token 作为 API Key 上传
  //   false → 不做任何操作（默认）
  "sub2api_auto_upload": false,

  // Sub2API 管理员 API Key（后台"管理员 API Key"处获取）
  "sub2api_admin_api_key": "",

  // Sub2API 后端地址，只填 origin，不要带路径
  //   正确：https://api.example.com
  //   错误：https://api.example.com/admin
  "sub2api_base_url": "",

  // 绑定的分组名称数组（脚本启动时自动查询 Sub2API 分组列表，按名称匹配 ID）
  //   填写分组名称，如 ["ChatGPT主号池", "Claude中转站"]
  //   名称匹配不区分大小写
  //   如果找不到匹配的名称，会打印可用分组列表并跳过
  "sub2api_group_names": [],

  // 平台类型：anthropic / openai / gemini / antigravity
  "sub2api_platform": "anthropic",

  // 上游 API 地址
  "sub2api_upstream_base_url": "https://api.anthropic.com",

  // 并发数
  "sub2api_concurrency": 1,

  // 优先级（数字越小优先级越高）
  "sub2api_priority": 1,

  // 池模式：random / round_robin
  "sub2api_pool_mode": "random",

  // 池模式重试次数
  "sub2api_pool_mode_retry_count": 3,

  // ========== 输出格式配置 ==========

  // 输出格式：cpa / sub2api / both
  //   cpa     → 仅输出 CPA 格式（codex_tokens/*.json）
  //   sub2api → 仅输出 Sub2API 格式（sub2api_tokens/*.sub2api.json）
  //   both    → 两种格式都输出
  // 注意：sub2api_auto_upload = true 时，自动强制生成 Sub2API 格式
  "output_format": "cpa",

  // Sub2API 格式输出目录
  "sub2api_output_dir": "sub2api_tokens"
}
```

---

## 配置场景示例

### 场景一：本地代理（Clash / V2Ray / 机场）

```json
{
  "proxy_type": "static",
  "proxy": "http://127.0.0.1:7890"
}
```

### 场景二：固定远程代理（带认证）

```json
{
  "proxy_type": "static",
  "proxy": "http://user123:pass456@1.2.3.4:8080"
}
```

### 场景三：动态代理 API（如 1024proxy）

```json
{
  "proxy_type": "dynamic",
  "proxy_api_url": "https://white.1024proxy.com/white/api?region=JP&num=1&time=10&format=1&type=txt",
  "proxy_api_retry": 3
}
```

> **注意**：动态模式下每个账号/线程都会调用一次 API，并发多个账号时确保 API 配额足够。

### 场景四：链式代理

```json
{
  "proxy_type": "static",
  "proxy": "http://127.0.0.1:7890",
  "proxy_chain_enabled": true,
  "proxy_chain_upstream_proxy": "1.2.3.4:8080:user-region-JP-sid-{session}-t-5:pass",
  "proxy_chain_dynamic_per_account": true
}
```

流量路径：`脚本 → 127.0.0.1:7890（Clash）→ 上游 proxy:8080 → 目标网站`

### 场景五：注册成功后自动上传到 Sub2API 中转站

```json
{
  "proxy_type": "static",
  "proxy": "http://127.0.0.1:7890",
  "sub2api_auto_upload": true,
  "sub2api_admin_api_key": "sk-admin-xxxxxxxxxxxx",
  "sub2api_base_url": "https://api.your-sub2api.com",
  "sub2api_group_names": ["ChatGPT主号池", "Claude中转站"],
  "sub2api_platform": "anthropic",
  "sub2api_upstream_base_url": "https://api.anthropic.com"
}
```

> **说明**：脚本启动时自动调用 `GET /api/v1/admin/groups` 查询 Sub2API 的所有分组，打印分组列表（名称 + ID + 平台），然后将 `sub2api_group_names` 中的名称匹配为 ID。注册成功后，将 Codex Refresh Token 作为 API Key 上传到 Sub2API 并绑定对应分组。
>
> **注意**：
> - 上传失败**不会**导致注册失败，只会在日志中打印警告
> - 开启自动上传时，**自动强制生成 Sub2API 格式文件**（`sub2api_tokens/*.sub2api.json`），无需额外配置 `output_format`
> - 需要先在 Sub2API 后台手动创建分组，再将分组**名称**填入 `sub2api_group_names`
> - 管理员 API Key 在 Sub2API 后台 → 管理员 API Key 处生成，非普通用户 Key

### 场景六：仅输出 Sub2API 格式文件（不上传）

```json
{
  "proxy_type": "static",
  "proxy": "http://127.0.0.1:7890",
  "output_format": "sub2api",
  "sub2api_output_dir": "sub2api_tokens"
}
```

> **说明**：设置为 `sub2api` 模式后，注册成功会生成 `sub2api_tokens/*.sub2api.json` 文件。格式参考 [CPA2sub2API](https://github.com/gtxx3600/CPA2sub2API) 转换器，包含完整的 `credentials`（access_token/id_token/refresh_token/expires_at/plan_type/organization_id 等）。
>
> 可用 `output_format` 值：
> - `"cpa"` — 仅 CPA（默认，向后兼容）
> - `"sub2api"` — 仅 Sub2API 格式
> - `"both"` — 两种格式都输出

---

## 输出文件

| 路径 | 说明 |
|------|------|
| `registered_only.txt` | 注册成功：邮箱----密码----rt----refresh_token |
| `register_only_failed.txt` | 注册失败：错误原因 |
| `chatgpt_sessions/` | ChatGPT Web 会话缓存（cookie + access_token） |
| `codex_tokens/` | CPA 格式 Codex 令牌文件（refresh_token / access_token / id_token） |
| `sub2api_tokens/` | Sub2API 格式文件（`output_format: "sub2api"` 或 `"both"` 时生成） |

## 环境变量

| 变量 | 说明 |
|------|------|
| `PROXY` | 代理地址（优先级 > 配置文件） |
| `PROXY_CHAIN_ENABLED` | 强制开启链式代理（1/true/yes/on） |
| `PROXY_CHAIN_UPSTREAM_PROXY` | 上游代理模板 |
| `PROXY_CHAIN_DYNAMIC_PER_ACCOUNT` | 每账号独立粘性 session（1/true/yes/on） |
