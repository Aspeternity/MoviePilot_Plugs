# MoviePilot_Plugs

[![Plugin checks](https://github.com/Aspeternity/MoviePilot_Plugs/actions/workflows/checks.yml/badge.svg)](https://github.com/Aspeternity/MoviePilot_Plugs/actions/workflows/checks.yml)

MoviePilot V3 插件仓库，由 [Aspeternity](https://github.com/Aspeternity) 维护。

| 插件 | 插件 ID | 当前版本 | 最低 MoviePilot 版本 |
| --- | --- | --- | --- |
| NodeSeek 论坛签到 | `NodeSeekV3` | `1.6.1` | `3.2.8` |

## 安装

1. 在 MoviePilot 的插件市场设置中，保留原有仓库，并添加本仓库地址：

   ```text
   https://github.com/Aspeternity/MoviePilot_Plugs
   ```

2. 刷新插件市场，搜索「NodeSeek 论坛签到」，确认来源为本仓库后安装。
3. 打开插件配置，选择 Cookie 来源并验证登录，设置签到计划后启用定时签到并保存。

仓库代码通过 `main` 分支和 `package.v3.json` 提供，无需下载 ZIP 后手动安装，
也不要求先创建 GitHub Release。MoviePilot 需要能够访问 GitHub。

已有本地安装迁移时，先备份原始源码和插件配置，使用插件的来源选择或切换入口，
按界面提示选择本仓库。保留插件 ID，不要通过卸载插件来进行首次迁移。
不同版本的来源管理界面可能不同，以实际界面和日志为准。

## NodeSeek 论坛签到

使用 MoviePilot 的浏览器能力执行签到，提供 Vuetify JSON 配置表单，无额外运行依赖。

- 默认每天北京时间 `08:00` 签到，可使用 Cron 或当天随机时段。
- 随机时段分别设置开始和结束时间，开始必须早于结束，且位于同一天。到达结束时间后，尚未执行的当日任务跳过。
- 重启或重载时沿用仍未到期的随机时间。仅当原时间已过、仍在时段内且当天尚未执行时，从剩余时段重新抽取。
- Cron 新编辑的星期按编辑器的编号执行，0 和 7 表示周日。旧数字规则沿用原来实际执行的星期，配置表单显示对应日期。
- 支持随机或固定奖励、结果通知、系统代理，以及保存配置后执行一次。
- 「查看数据」展示今日签到、今日奖励、最近签到和下次执行，签到历史每页 10 条，保留最近 30 个北京时间自然日。
- 仅在确认签到成功或已签到后记录当日完成状态；旧版本未保存的历史无法补回。

### Cookie 来源

可选择「手动输入 Cookie」或「同步 CookieCloud」。

手动模式需要填写有效的 NodeSeek Cookie。CookieCloud 模式复用 MoviePilot 的配置，
按宿主同步间隔刷新独立快照，并在每次签到前读取 `nodeseek.com` 分组。
云端读取失败时，本次签到失败，不回退到手动 Cookie。

「验证手动 Cookie」和「验证 CookieCloud 登录」使用表单当前的 User-Agent 和代理设置确认登录。
验证通过后可以开启定时签到或「保存后立即签到一次」，保存时插件会检查实际配置。
更换登录方式、手动 Cookie、代理或 User-Agent 后，需重新验证再启用。
运行时若 NodeSeek 明确返回未登录，定时签到会暂停，更新 Cookie 并验证后可重新启用。
临时网络故障会记录本次结果，保留下次排期。使用 CookieCloud 时，请先在浏览器登录 NodeSeek，
再同步 CookieCloud。

宿主 CookieCloud 按站点聚合子域，并丢弃原始 Cookie 的域、路径、有效期等属性；
插件仅将目标分组注入 NodeSeek 地址。真实 Cookie 是登录凭证，
请只保存在 MoviePilot 的运行配置中，不要粘贴到仓库、Issue 或公开日志。

### 网络与浏览器

启用系统代理前，请先配置 MoviePilot 的代理。浏览器、网络、Cookie 或站点验证异常
都会导致签到失败。插件不处理或绕过站点验证。

本实现参考 NodeSeek 的 `/api/attendance/board` 和 `/api/attendance` 请求形态，
站点接口或页面变化后可能需要适配。

## 本地开发

```text
plugins.v3/nodeseekv3/   插件源码
tests/                  离线行为测试
package.v3.json         插件市场索引
requirements-dev.txt    本地测试依赖
.github/workflows/      自动检查
```

推荐在本地 Linux 或 WSL 修改代码，并在实际 MoviePilot 宿主中联调。
测试环境和运行环境的准备、版本维护规则见 [开发说明](CONTRIBUTING.md)。

本仓库也可注册为本地插件源：将仓库目录映射到容器内，
在 `PLUGIN_LOCAL_REPO_PATHS` 中填写容器内的仓库根目录。
启用 `PLUGIN_AUTO_RELOAD=true` 后可监视已安装插件的源码变化。
热加载是全局设置，修改插件源码可能触发其重新加载。

使用本地插件源联调时，请在日志中确认实际使用的源码来源；
本地仓库与 GitHub 之间的文件仍需通过 Git 拉取、推送或其他同步方式传递。
