# 首次使用

先从 [首页能力表](../README.md#能做什么)选一个目标。完整 checkout 才有功能代码；只安装 Skill 不等于服务可运行。

## 1. 检查这台电脑

在仓库根目录运行：

```sh
python3 wechat_ai.py doctor all
```

`doctor` 只报告环境，不安装、初始化账号或改变网络。确定目标后也可以只检查对应服务，例如 `python3 wechat_ai.py doctor media`。

| 目标 | 系统条件 |
| --- | --- |
| 聊天导出 | macOS 15+ 与 Swift Command Line Tools；Windows 10/11 x64 聊天仍是实机待验的源码预览 |
| 媒体转 MP3 | macOS 或原生 Windows；来源网站与微信播放链路分别验收 |
| 公众号文章 | macOS，使用本人可见的微信会话 |
| 直播回放 / 课程合集 | Apple Silicon Mac、本地 MLX 模型、CPython 3.12/3.13；目前是开发预览 |

普通 macOS 服务使用标准 CPython 3.10–3.13。Windows 聊天要求 CPython 3.12 x64 和 VC++ 运行库；在原生 PowerShell 操作，不以 WSL 代替 Windows 实机验证。套件不自带 Python。

## 2. 只安装选中的服务

```text
python3 wechat_ai.py install chat
python3 wechat_ai.py install media
python3 wechat_ai.py install articles
python3 wechat_ai.py install replay
```

按所选目标执行其中一项；课程复用 `replay` 环境。安装器隔离依赖，不自动登录、下载模型、读取旧项目或开启调度。聊天/媒体保留固定版本与发行哈希；公众号和直播的传递依赖尚未全量锁定。

## 3. 给出素材并运行

| 目标 | 你提供 | Codex 选择 |
| --- | --- | --- |
| 聊天 | 会话、绝对时间范围和类型；本人登录微信 | [聊天 Skill](../skills/wechat-ai-chat/SKILL.md)；首次账号访问另用 [初始化 Skill](../skills/wechat-ai-chat-setup/SKILL.md) |
| 公众号 | 一条公开文章链接、起始日期或“全部历史”；本人处理登录/验证码 | [文章归档](../skills/wechat-ai-articles/SKILL.md)或[订阅更新](../skills/wechat-ai-subscriptions/SKILL.md) |
| 直播 | 账号名及一条可访问的回放链接；本人登录并打开种子一次 | [直播 Skill](../skills/wechat-ai-replay-word/SKILL.md)；临时接入需先核对服务、域名、期限与回滚 |
| 课程 | 本地清单、授权音视频和本地模型 | [课程 Skill](../skills/wechat-ai-courses/SKILL.md)；先核对层级与数量 |
| MP3 | 有权处理的链接或本地文件 | [媒体 Skill](../skills/wechat-ai-media/SKILL.md) |

根入口形式为 `python3 wechat_ai.py run <服务> -- <动作及参数>`；具体动作由选中的 Skill 给出。公众号的本地控制台只监听回环地址。macOS 视频号链接若要自动发送到文件传输助手，需取得本次明确授权；Windows 保留手动播放确认。

## 4. 对账交付

先用一个获授权样本核对数量、顺序、完整解码、正文及最终文件，再扩大范围。直播 Word 和课程 Word 还要渲染检查；机器转写完成不等于人工逐字校听或版面验收。[验证记录](VALIDATION.md)

恢复任务时先检查原任务身份、哈希和活动 worker。登录、验证码、付费墙或限流使对应来源暂停。旧项目数据与账号状态不会自动迁移；[迁移边界](MIGRATION.md)另行说明。
