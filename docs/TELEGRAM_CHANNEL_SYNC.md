# Telegram 频道自动归档到 GitHub

`tools/telegram_channel_to_github.py` 使用 Telegram Bot API 长轮询获取指定频道的新消息，并为每条可公开的文本消息创建一个 GitHub Markdown 文件。

## 安全默认值

- 只接受一个指定的频道 ID 的 `channel_post`；
- 不改变或删除既有 webhook；若该 Bot 已配置 webhook，程序会停止并提示改用 webhook 方案；
- 图片、视频、音频、文档、贴纸等媒体消息默认跳过，不上传到 GitHub；
- 私钥、Telegram/GitHub/AWS 令牌、疑似 API 密钥、邮箱、手机号和中国身份证号会阻止自动公开；
- 每篇归档均带有“看么科技客服 @hwxc129 / 看么科技频道 @hwxc131”页脚；
- 每篇归档标题自动取频道正文的第一行，并标记为“自动化采集频道发送”；
- 每篇成功写入 GitHub 的归档会通过 Bot 通知指定 Telegram 用户；
- 使用本地状态文件和固定的 `message_id` 路径去重；重复运行不会覆盖已归档消息；
- GitHub 写入串行执行，避免 Contents API 的并发冲突。

基础规则不能替代人工审核。涉及个人资料、版权、未审核图片或年龄适宜性不明的内容，请人工确认后再发布。

## 准备工作

1. 在 BotFather 创建一个专用 Bot，并将它添加为目标频道的管理员；
2. 取得目标频道的数字 ID（通常以 `-100` 开头）；
3. 创建仅授权给 `hwxc129/soulai-static-client` 的 GitHub fine-grained token，并授予 `Contents: Read and write`；
4. 将 `TELEGRAM_BOT_TOKEN` 和 `GITHUB_TOKEN` 存入服务器的密钥管理服务或权限为 `600` 的环境文件，绝不提交到仓库。

Telegram 的 `getUpdates` 与 webhook 不能同时使用；频道新帖会通过 `channel_post` Update 提供给 Bot。GitHub Contents API 使用 Base64 内容创建文件，并要求有仓库 Contents 写权限。[Telegram Bot API](https://core.telegram.org/bots/api#getupdates) [GitHub Contents API](https://docs.github.com/en/rest/repos/contents#create-or-update-file-contents)

## 本地演练

无需任何真实凭据即可运行已附带的安全测试数据：

```bash
python3 tools/telegram_channel_to_github.py \
  --channel-id -1001234567890 \
  --fixture tests/fixtures/telegram_channel_updates.json \
  --dry-run \
  --once
```

输出会显示计划创建的 `telegram-posts/YYYY/MM/DD/<message_id>.md`，不会请求 Telegram/GitHub，也不会生成状态文件。

## 正式运行

在受控服务器中安全读取凭据，例如：

```bash
read -rs TELEGRAM_BOT_TOKEN && export TELEGRAM_BOT_TOKEN
read -rs GITHUB_TOKEN && export GITHUB_TOKEN

python3 tools/telegram_channel_to_github.py \
  --channel-id -100你的频道ID \
  --repo hwxc129/soulai-static-client
```

不要把令牌填进命令行、日志、截图或 Git 提交。程序会每 45 秒长轮询一次；建议交给 systemd、Docker 或其他具备自动重启能力的运行环境托管。终止信号会在当前请求结束后安全退出。

## 已配置的 GitHub Actions 自动任务

仓库内的 `.github/workflows/telegram-channel-sync.yml` 已设置为每 5 分钟检查频道 `-1002621410281`。它使用 GitHub 自动提供的 `GITHUB_TOKEN` 写入本仓库，不需要额外创建 GitHub PAT。

该自动任务会把 Update 偏移量写入 `telegram-posts/.sync-state.json`，其中不含 Bot Token、频道正文或用户资料；这样临时 Runner 在下一轮仍能从正确位置继续。首次运行只会处理 Bot 已收到的未确认频道更新。

只需在仓库 **Settings → Secrets and variables → Actions** 中设置名为 `TELEGRAM_BOT_TOKEN` 的 Secret。没有该 Secret 时任务会安全跳过，不会读取或发布内容。GitHub 的定时任务最短为每 5 分钟，繁忙时可能延迟；公共仓库连续 60 天无活动时会自动停用定时任务，因此对实时性或长期稳定性有要求时，应使用服务器部署方案。[GitHub Actions 定时任务说明](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)

成功归档后，Bot 会向已配置的通知用户发送 GitHub 链接。请先在 Telegram 中向该 Bot 发送一次 `/start`；否则 Telegram 不允许 Bot 主动私聊该用户，任务会保留未确认 Update 并在下一轮重试。

## 运维说明

- `--once`：只取一批更新后退出，适合定时任务和排查；
- `--state-file`：默认 `.runtime/telegram-github-state.json`，保存下一条 Update 偏移量，已被 `.gitignore` 忽略；
- `--path-prefix`：默认 `telegram-posts`，可更改归档目录；
- `--github-state-path`：将去重偏移量保存到仓库，适合 GitHub Actions 等无持久磁盘的 Runner；
- `--dry-run`：验证配置和内容规则但不写 GitHub；
- `--fixture`：只能与 `--dry-run` 一起使用，避免测试数据被公开；
- 如改错频道 ID，可停止程序、检查状态文件后按需移除该本地状态文件，再重新启动。不要删除 GitHub 已归档的历史文件。
