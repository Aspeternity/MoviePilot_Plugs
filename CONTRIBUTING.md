# 开发说明

本仓库只保存插件源码。插件实际运行在 MoviePilot V3 的 Python 进程中。

## 本地检查

离线测试使用 Python 3.14，与当前 MoviePilot V3 的 Python 主版本保持一致。
测试通过模拟宿主 SDK 和浏览器验证插件行为，不连接真实站点、不执行真实签到。

在 Linux 或 WSL 中执行：

```bash
uv venv --python 3.14 .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python -m pytest -q
git diff --check
```

没有安装 uv 时，可用已有的 Python 3.14 执行 `python3.14 -m venv .venv`，
再使用 `.venv/bin/python -m pip install -r requirements-dev.txt` 安装测试依赖。

离线测试不能替代宿主联调。涉及 SDK、浏览器、定时任务或页面的改动，
还需在实际使用版本的 MoviePilot 中检查插件加载、停止与重载。

## 修改与发布

1. 修改 `plugins.v3/nodeseekv3/` 下的源码，按需要维护 `tests/`。
2. 运行离线检查，并在 MoviePilot 中验证实际行为。
3. 发布新版时，同时更新 `plugin_version`、`package.v3.json` 中的 `version`，
   并将当前版本更新记录放到 `history` 第一项。
4. 将验证后的代码推送或合并到 `main`；MoviePilot 插件市场从该分支读取源码。

保留插件 ID `NodeSeekV3` 和目录名 `nodeseekv3`，避免影响已有安装的身份、配置和数据。
新增插件也使用 `plugins.v3/<插件类名的小写>/__init__.py` 和根目录的 `package.v3.json`。

## 提交身份与运行配置

提交前，为当前仓库配置自己的 GitHub 用户名和 GitHub 提供的 noreply 邮箱。
邮箱可在 GitHub 的 Settings → Emails 中查看；不要在公开提交中使用个人邮箱。
GitHub 网页编辑产生的提交也应使用账号的隐私邮箱设置。

真实 Cookie、CookieCloud 凭据、Token 和数据库只保存在运行环境中。
测试中的 `example`、`mock-secret` 等字符串为虚构数据。

本仓库的自动检查只读取源码和执行测试，不会创建代码提交。
