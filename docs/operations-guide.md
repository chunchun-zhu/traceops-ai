# 账号启用与备份恢复

## 启用账号模式

先停止服务，创建首位管理员（终端隐藏密码，不提供默认密码）：

```powershell
python scripts/accounts.py --db data/suzhang.db --username admin --name 管理员
```

配置 `.env`：

```dotenv
SUZHANG_AUTH_MODE=accounts
SUZHANG_COOKIE_SECURE=true
SUZHANG_SEED_DEMO=false
```

生产用 HTTPS，保留 Secure。仅本机 HTTP 演示设置 `SUZHANG_COOKIE_SECURE=false`。启动后页面会要求登录；管理员在“账号与审计”中创建操作员和独立审核员，密码至少 12 字符。账号角色/状态保存立即撤销该账号的全部会话（含自己）。最后一位有效管理员不能降级或停用。建议保留两位独立管理员。

管理员可在 API 文档调用 `POST /api/admin/users/{id}/password` 重置密码，请求 `{password: "新的密码"}`，须使用已登录 Cookie 和本站 Origin。重置撤销目标账号的会话。丢失所有管理员凭据后不可重复 bootstrap；需从可信备份恢复或由数据库管理员执行受控恢复。

标准录入 → 待审 → 另一位审核员核对原文、型号、单位和范围 → 批准 → 可用于复测。提交人不能自审（包括管理员）。拒绝后的更改需要重新提交。账号模式不认可旧版手填审核人，旧标准需重新提交独立审核，已完成会话原快照保留。

账号模式覆盖共享 Token。API 调用者使用 `/api/auth/login` 的 Cookie；浏览器自动附带 Origin，脚本须设置与服务公开地址完全一致的 Origin。不要开放任意 Origin。反向代理的 Host/协议须正确转发，仅信任指定代理地址，避免来源校验误判。stdio MCP 属于有本地数据库权限的可信运行程序，未接入 HTTP 登录体系；不要向未授权人员提供本机/MCP/数据库访问。

## 停服备份

先停止 FastAPI、MCP、导入脚本和所有数据库写入程序；不会复制仍运行的 FastAPI/任务工作线程。使用新的备份目录：

```powershell
python scripts/maintenance.py backup --db data/suzhang.db --destination data/backups/20261010-01
python scripts/maintenance.py verify --bundle data/backups/20261010-01
```

首次须至少正常启动过服务。工具成套生成 `suzhang.db`、`suzhang.db.checkpoints`、`manifest.json`，支持 SQLite WAL 数据；不会只复制主库漏掉检查点。目标存在时拒绝覆盖。备份包含手册、人员密码摘要和任务原始文件，请保存到受访问控制的目录并设置离线副本；校验和只能检测损坏。`.env`、API 密钥配置和模型权重需单独保管。

## 恢复演练与切换

```powershell
python scripts/maintenance.py restore --bundle data/backups/20261010-01 --destination data/recovery-01
$env:SUZHANG_DB_PATH='data/recovery-01/suzhang.db'
python -m uvicorn app.main:create_app --factory --host 127.0.0.1 --port 8080
```

恢复目录必须尚不存在，工具不会覆盖运行库。账户密码保留，登录会话清空；工作人员重新登录后核对设备、手册、任务和未完成案例，继续维修与复测。后台任务按原恢复逻辑重新排队/幂等重放；模型密钥和端点通过原 `.env` 恢复。确认验收后将持久配置指向恢复库；若不合格，停服并切回原库。不要同时运行原库和恢复库处理同一实际事件。

自动化恢复测试已覆盖：未完成案例恢复后继续关闭、历史事件保留、账号与任务保留、旧会话失效、校验失败拒绝、原目录不覆盖。需结合实际数据量和备份存储介质定期演练；本轮没有测量生产 RPO/RTO。
