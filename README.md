# 微信 AI 服务

在本地把微信聊天、公众号文章和视频内容整理成可保存、可查找的文件。告诉 Codex 你要处理什么，它会选择对应能力，先检查当前电脑，再按需运行。

> **状态：开发预览。** 代码已公开，但不同电脑、微信版本和账号的完整流程仍需逐项实测。[查看已验证范围](docs/VALIDATION.md)

## 能做什么

| 你的目标 | 得到什么 | 当前适用范围 |
| --- | --- | --- |
| **归档微信聊天** | 按会话、时间、类型导出消息与媒体；可把选定语音合成 MP4 | macOS；Windows 聊天为原生源码预览 |
| **归档公众号文章** | 从一篇公开文章定位公众号，收集可见历史；保存 Markdown，并为新增文章生成 Word | macOS 可见会话；支持“添加公众号”和“更新全部公众号到今天” |
| **整理直播回放** | 自动盘点可访问的账号回放，本地转写并合成**一份带时间戳的完整 Word**；面板显示进度 | Apple Silicon 开发预览；目录临时接入需单独授权 |
| **整理课程合集** | 用授权的本地音视频逐课转写，保留讲义，按合集生成完整 Word | Apple Silicon 开发预览；不自动读取课程网站账号 |
| **提取音频** | 把有权处理的链接或本地媒体转成 MP3，并验证文件 | macOS / Windows；具体来源按对应 Skill 验收 |

这里的“完整”指已取得素材的机器转写正文，不保证识别结果逐字准确；隐藏、已删除或无权访问的内容不会凭空补齐。[能力与平台详情](docs/CAPABILITY_MAP.md)

## 从这里开始

1. 把仓库克隆到独立目录，在仓库根目录检查环境：

   ```sh
   git clone https://github.com/delu543/wechat-ai-services.git
   cd wechat-ai-services
   python3 wechat_ai.py doctor all
   ```

2. 告诉 Codex 你的**目标和素材**，例如“归档这个公众号从某日期起的文章”或“把这个账号可访问的直播回放整理成一份 Word”。提供相应文章/回放链接、聊天范围或本地文件。Codex 会先读 [首次使用流程](docs/FIRST_RUN.md)，只安装所需服务，并在需要登录、选择模型或临时接入时说明具体步骤。

已克隆仓库也可单独安装入口 Skill，让 Codex 自动选择能力：

```sh
npx -y skills add delu543/wechat-ai-services --skill wechat-ai-services -a codex -y
```

Skill 是使用说明；运行功能仍需要完整仓库及所选服务的依赖。全部 8 个独立 Skill 见 [Skill 导航](skills/README.md)。

## 仓库导航

| 位置 | 用途 |
| --- | --- |
| [首次使用](docs/FIRST_RUN.md) | 系统条件、选择服务、安装与首次验收 |
| [能力与平台](docs/CAPABILITY_MAP.md) | 交付物、支持范围和详细验证边界 |
| [Skills](skills/README.md) | 按目标选择独立能力及操作契约 |
| [components](components/README.md) | 已审阅的聊天、公众号、媒体功能源码与来源说明 |
| [更多文档](docs/README.md) | 课程清单、兼容迁移、来源和验证记录 |

根目录 [wechat_ai.py](wechat_ai.py) 是统一入口：`doctor` 检查环境，`install` 安装选定服务，`run` 执行对应能力。安装不会自动登录微信、迁移旧数据或启动后台任务。

## 数据与许可

账号状态、媒体、数据库、Word 和测试证据保存在本机，不随仓库发布；各能力的访问授权分别判断。[隐私与迁移边界](docs/MIGRATION.md)

本仓库包含不同来源与许可状态的组件，**公开可读不等于整体已获开源使用许可**。[许可边界](LICENSE.md) · [来源记录](docs/SOURCES.md)
