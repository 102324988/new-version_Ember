# 一键故障诊断

在项目根目录双击 **`diagnose.bat`**。窗口会保留结果，报告写入：

```text
output/diagnostics/日期_时间_标识/report.txt
output/diagnostics/日期_时间_标识/report.json
```

优先看 `FAIL` 及紧随其后的建议。`WARN` 是待核对线索，可能来自已经恢复的历史日志；`INFO` 说明检测边界。修复后再运行即可比较。报告目录已由 Git 忽略，可提供 `report.txt` 帮助排查，不需要发送 `.env`、访问码或聊天日志。

## 默认检查

- 自动选择模式：存在 `deploy/service.env` 则检查容器服务，否则检查本机 Python 模式。
- 检查配置缺项和示例占位符；不打印配置值。
- 容器模式检查 Docker 引擎、Compose 配置、应用及 PostgreSQL 容器运行情况；从实际运行容器检查依赖。
- 本机模式依次使用 `EMBER_PYTHON`、项目 `.venv`、PATH 中的 Python，检查版本和依赖；不要求容器模式安装本机 Python。
- 检查实际运行环境的模型配置、Edge-TTS 版本、状态/聊天/工作记忆 JSON、失败写入待办数量。
- 使用只读连接检查数据库认证、`vector` 扩展和必要业务表；不读取聊天正文，不建表、不修复序列、不清空数据。
- 检查 HTTP 存活与就绪状态。容器最近 200 行日志只在内存中做错误分类，不导出原始日志或聊天。

脚本不自动启动/重启 Docker 或后端，不安装依赖，不修改配置，不登录网页，不调用生成、向量或语音供应商接口。报告只写入 `output/diagnostics`。诊断不是完整测试套件，也不能保证一次找到所有应用逻辑问题。

## 额外网络检查

```bat
diagnose.bat -Network
```

额外检查配置中模型端点的 DNS、TCP 和 HTTPS TLS 连接，不传密钥、不发送生成请求。同一主机复用检查结果。可达不能证明密钥、模型权限或额度有效；网络错误也可能涉及代理和证书，需要结合实际供应商错误核对。不会生成语音或证明浏览器自动播放权限正常。

网络探针使用直连，不走 HTTP_PROXY／HTTPS_PROXY；如果模型调用依靠代理，直连失败不能代表代理调用失败。

## 指定运行模式

```bat
diagnose.bat -Mode Service
diagnose.bat -Mode Local
diagnose.bat -Mode Service -ProjectName ember-service -ConfigFile deploy/service.env
```

默认检查本机 `127.0.0.1`：容器端口取配置的 `EMBER_SERVICE_PORT`，默认 8080；本机模式默认 8000。只绑定特定局域网地址、单独反代或非默认本机端口时，HTTP 检查可能无法代表实际入口，需按启动配置另行核对。本机模式读取指定配置，已有进程环境变量仍优先；容器内部探针检查运行时环境，因此修改文件但未重启时可能与磁盘配置不同。

有多个 Python 时，可先设置 `EMBER_PYTHON` 为实际运行后端的解释器路径，避免诊断另一个环境。

自动化可直接运行 PowerShell 脚本，不停留窗口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File utils/diagnose.ps1 -Mode Service
```

或在调用 bat 前设置 `EMBER_DIAGNOSTICS_NO_PAUSE=1`。退出码 0 表示没有检测到 FAIL，1 表示存在失败项；0 不代表所有模型能力都已实测。

## 常见定位

| 结果 | 优先排查 |
|---|---|
| Docker 不可用 | 打开 Docker Desktop 等待引擎就绪，容器诊断不自行启动服务 |
| 配置缺失、已有数据卷 | 恢复原配置；不要重新生成密码或删除卷 |
| 数据库认证失败 | 核对实际用户密码；修改环境变量不会自动修改已有 PostgreSQL 用户密码 |
| 缺 vector 或业务表 | 核对目标数据库、pgvector 镜像和初始化日志，保留业务数据 |
| 接口超时 | 加 `-Network` 区分连接问题；权限/额度还需供应商错误码 |
| 语音错误或自动播放无声 | 核对 edge-tts 版本；手动重播正常时检查站点声音权限和网页交互 |
| 待处理写入不为零 | 修复存储或向量服务后等待重试，不删除待办记录 |
| HTTP 503 | 后端未准备好或存储不可用，结合探针结果排查 |

验证：Windows PowerShell 5 实机容器只读检查已通过；探针单元检查使用模拟驱动和临时文件，不连接业务数据库。子进程有超时，探针无输出、非成功退出或格式错误均报告失败，不会当成正常结果。

新增 6 项独立单元检查通过；不存在的容器项目名会明确失败，本机合成错误数据库配置也会失败，包含中文和空格的路径已验证。测试没有停止或修改正在运行的业务容器。
