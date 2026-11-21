自用
# Agentspace / Bard 自动监控

自动检查 https://status.oneapi.im/ 的顶部「通道」概览卡片。每次分别检查 `agentspace` 和 `bard`：

- **右上角整体状态为「健康」**，且
- **卡片「容量」列数值之和大于 0（∞ 也视为大于 0）**，

才会触发邮件提醒。下方模型子卡片的「正常 · 1 worker」不算作整体健康；「队列」值不算容量。

每个通道**只在从不满足条件变为满足条件时发一次**；持续健康不会反复发送。若之后休整或容量归零，再次恢复则重新提醒。

## 如何部署（没有编程经验也可以）

1. 在 GitHub 新建仓库：点右上角 **+ → New repository**，仓库名可填 `oneapi-status-alert`。优先选 **Public**（GitHub 公共仓库的标准托管 Actions 用量免费；邮箱密码不会放在公开代码里）。点 **Create repository**。
2. 将本压缩包**解压**，在仓库点击 **Add file → Upload files**，把 `monitor.py`、`README.md` 和 `.github` 文件夹拖到上传区域，检查最终的工作流路径必须是 **`.github/workflows/monitor.yml`**。点击 **Commit changes**。若浏览器不接收隐藏文件夹，可以在 GitHub 的 **Add file → Create new file**，在文件名中输入 `.github/workflows/monitor.yml`，将本地文件内容复制进去并提交。
3. 进入仓库 **Settings → Secrets and variables → Actions → New repository secret**，创建下列密钥：
   - `SMTP_HOST`：发件邮箱 SMTP 服务器，比如 Gmail 用 `smtp.gmail.com`，QQ 邮箱用 `smtp.qq.com`。
   - `EMAIL_USER`：发件邮箱，例如 `you@gmail.com`。
   - `EMAIL_PASSWORD`：发件邮箱的**应用专用密码 / SMTP 授权码**（不是网站登录密码）。Gmail 必须先开启两步验证，再生成应用专用密码；QQ 邮箱在邮箱设置中开启 SMTP 并取得授权码。
   - `EMAIL_TO`：接收提醒的邮箱地址（可以和发件地址一样）。
   - 可选：`SMTP_PORT`，默认 `465`（SSL），通常无须设置。
4. 打开仓库 **Actions → Monitor agentspace and bard → Run workflow → Run workflow**，点开运行记录。全绿表示运行正常；如果 `bard` 仍是「健康 + 容量>0」，第一次运行会立即发送测试性质的实际提醒邮件。
5. 此后 GitHub 每约 **5 分钟**自动检查（不是秒级实时；GitHub 可能延迟甚至跳过个别计划任务）。运行结果在 Actions 中查看。邮件可能进入垃圾邮箱。

**注意**：GitHub 的公共仓库计划工作流在 **60 天无仓库活动**后可能自动停用，请定期检查 Actions 是否仍在执行。持续监控时可以定期手动提交一次修改来维持活动。

## 技术说明

- 使用 Playwright Chromium 读取网站页面上实际呈现的状态和容量，不假设网站存在某个 API。
- 读取失败时不会擅自判定「健康」，也不会重置旧状态；若无法可靠定位容量数值，运行记录会提示错误并可下载 `status-page-debug` 截图。
- `state.json` 会被 GitHub Actions 自动提交，以便下次运行知道是否已经提醒过。用户无需手动创建它。
- 网站当前无法从开发环境直接访问，因此读取逻辑已在模拟 HTML 下测试，但仍需要在实际 GitHub Actions 上运行一次验证；网站若更改前端布局，可能需要更新解析器。
- 仓库公开时，代码和状态记录可见，但邮件地址和授权码必须只放在 GitHub Secrets，切勿写入 README 或源代码。
