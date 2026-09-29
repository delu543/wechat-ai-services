# 微信 AI 服务

在本地把微信聊天、公众号文章和视频内容整理成可保存、可查找的文件。告诉 Codex 你要处理什么，它会选择对应能力，先检查当前电脑，再按需运行。

> **状态：开发预览。** 代码已公开，但不同电脑、微信版本和账号的完整流程仍需逐项实测。[查看已验证范围](docs/VALIDATION.md)

## 能做什么

- **微信聊天 → 消息与媒体档案。** 按会话、时间、类型导出；选定语音可合成 MP4。macOS；Windows 聊天为原生源码预览。
- **公众号文章 → Markdown 档案与增量 Word。** 从一篇公开文章定位账号、收集可见历史；支持“添加公众号”和“更新全部公众号到今天”。macOS 可见会话。
- **直播回放 → 一份带时间戳的完整 Word。** 自动盘点可访问的账号回放，本地转写并显示进度。Apple Silicon 开发预览；目录临时接入需单独授权。
- **课程合集 → 每合集完整 Word。** 按授权的本地音视频逐课转写并保留讲义。Apple Silicon 开发预览。
- **媒体 → 经验证的 MP3。** 处理有权使用的链接或本地文件。macOS / Windows；来源链路分别验收。

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

- [首次使用](docs/FIRST_RUN.md)：系统条件、服务安装与首次验收。
- [能力与平台](docs/CAPABILITY_MAP.md)：交付范围和详细验证边界。
- [Skills](skills/README.md)：按目标选择操作契约。
- [components](components/README.md)：聊天、公众号与媒体的功能源码。
- [更多文档](docs/README.md)：课程清单、迁移、来源和验证记录。

根目录 [wechat_ai.py](wechat_ai.py) 是统一入口：`doctor` 检查环境，`install` 安装选定服务，`run` 执行对应能力。安装不会自动登录微信、迁移旧数据或启动后台任务。

## 数据与许可

账号状态、媒体、数据库、Word 和测试证据保存在本机，不随仓库发布；各能力的访问授权分别判断。[隐私与迁移边界](docs/MIGRATION.md)

本仓库中项目所有者有权授权的源码、Skills 和文档采用 [MIT 许可](LICENSE.md)，允许使用、修改和再分发，并须保留版权与许可声明。第三方依赖、运行时二进制及模型仍遵守各自许可；[查看具体范围](docs/LICENSE_SCOPE.md) · [来源记录](docs/SOURCES.md)。
