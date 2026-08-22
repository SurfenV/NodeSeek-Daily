# NodeSeek 自动签到评论加鸡腿脚本

这是一个用于 NodeSeek 论坛的自动化脚本，包含签到、评论和加鸡腿功能。使用 Selenium 和 undetected-chromedriver 实现自动化操作。

强烈建议修改随机词。否则容易被举报被禁言。有能力的可以fork后自己定义改。

## 功能特点

- 自动签到（点击签到图标）
- 自动点击"试试手气"或"鸡腿 x 5"按钮（可配置）
- 随机选择帖子进行评论
- 自动给帖子加鸡腿（7天内的帖子）
- 随机评论内容（"bd"、"绑定"、"帮顶"）
- 支持 GitHub Actions 自动运行
- 支持无头模式（可配置）

## 环境变量配置

| 变量 | 必需 | 默认 | 说明 |
| --- | --- | --- | --- |
| `NS_COOKIE` | 是 | — | NodeSeek 的 Cookie |
| `NS_RANDOM` | 否 | `false` | `true` 点「试试手气」，`false` 点「鸡腿 x 5」 |
| `HEADLESS` | 否 | `true` | 是否无头模式。**CI 上应设为 `false` 并配合 Xvfb**，见下 |
| `NS_COMMENT_COUNT` | 否 | `3` | 每次评论多少个帖子，`0` 表示只签到不评论 |
| `CHROME_MAIN_VERSION` | 否 | 自动探测 | Chrome 主版本号，用于对齐 chromedriver |

## 本地运行

1. 克隆仓库
2. 安装依赖：`pip install -r requirements.txt`
3. 设置环境变量（可使用 .env 文件）
4. 运行脚本：`python nodeseek_daily.py`

## GitHub Actions 自动运行

1. Fork 本仓库
2. 到 Settings → Secrets and variables → Actions 添加 Secret `NS_COOKIE`
3. 可选：添加 Secret `NS_RANDOM`，或 Variable `NS_COMMENT_COUNT`
4. 到 Actions 页面手动跑一次「NodeSeek 每日签到」确认配置正确
5. 之后每天北京时间 00:37 自动运行

### 为稳定运行做的调整

这个仓库的 workflow 针对 CI 环境做过以下处理，改动前请先了解原因：

- **用 Xvfb 跑有头 Chrome，而不是 `--headless`。** NodeSeek 前面有
  Cloudflare，无头 Chrome 的指纹基本必被质询拦下。`HEADLESS` 在 CI 里
  默认设为 `false`，由 `xvfb-run` 提供虚拟显示。
- **不安装 `chromium-browser`。** 在 Ubuntu 22.04+ 上该 apt 包只是个
  snap 转发包，而 runner 里没有 snapd，装完不可用。直接使用 runner 镜像
  预装的 Google Chrome，并把主版本号传给 undetected-chromedriver。
- **Python 固定在 3.11。** undetected-chromedriver 3.5.5 仍然
  `import distutils`，在 3.12+ 会直接 ImportError。
- **失败自动重试 3 次**，退避等待，失败时把截图和页面源码作为 artifact
  上传，保留 7 天。
- **定时任务错峰到 :37**，避开 GitHub 整点排队导致的延迟和丢弃。
- **每月一次 keepalive 空提交**，避免仓库 60 天无活动后定时任务被
  GitHub 自动禁用。
- **评论默认收敛到 3 个帖子**（原为 20 个），词库也换得更自然。刷屏式
  评论极易被举报禁言，而账号一旦被禁言，签到就彻底停摆。

## 注意事项

- 请确保 Cookie 有效且具有足够的权限。Cookie 过期是签到失败最常见的原因，
  workflow 连续三次失败后会在日志里明确提示。
- 评论内容仍然是模板化的，**强烈建议按自己的习惯修改 `randomInputStr`**，
  或直接设 `NS_COMMENT_COUNT=0` 只保留签到与加鸡腿。
- 加鸡腿功能仅对 7 天内的帖子有效
