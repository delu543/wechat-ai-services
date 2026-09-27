# 微信 AI 服务

在 Codex 本地任务中，通过自然语言使用微信聊天导出、链接转 MP3、视频号回放转 Word、课程合集转录，以及公众号归档与增量更新。

这是三个既有项目的独立源码整合：保留各自的数据处理核心，用统一入口管理路由、安装和私有运行环境。**开发预览，未完成所有新电脑与微信版本的真实验收。**

## 新电脑直接发给 Codex

```text
请在一个新的独立目录安装并使用 https://github.com/delu543/wechat-ai-services。
先读取根 AGENTS.md、docs/FIRST_RUN.md 和所需 Skill，检查这台电脑，按我的目标安装对应能力。
不要覆盖旧项目、旧 Skills 或现有数据，不自动初始化账号、下载模型、发送微信消息或启用后台任务。
我的目标是：<聊天导出 / 链接转 MP3 / 直播转 Word / 课程合集 / 添加公众号 / 更新全部公众号到今天>。
需要我登录或决定具体隐私操作时，告诉我当前一步即可。
```

首次安装需要完整 checkout。仅安装 Skill 不包含全部功能代码或 Python/Swift 依赖。

```sh
git clone https://github.com/delu543/wechat-ai-services.git
cd wechat-ai-services
python3 wechat_ai.py doctor all
python3 wechat_ai.py install media
python3 wechat_ai.py run media -- preflight
```

安装独立 Skills（不会替换原来的 `wechat-local-export` 等名称）：

```sh
npx -y skills add delu543/wechat-ai-services --skill wechat-ai-services --skill wechat-ai-chat --skill wechat-ai-chat-setup --skill wechat-ai-media --skill wechat-ai-replay-word --skill wechat-ai-courses --skill wechat-ai-articles --skill wechat-ai-subscriptions -a codex
```

## 可用能力

| 用户目标 | 服务 / Skill | 当前边界 |
|---|---|---|
| 按群名、时间和类型导出聊天；语音合成 MP4 | `chat` / `wechat-ai-chat` | Mac 源码；Windows 原生预览，视频仅元数据 |
| 首次初始化当前聊天账号 | `wechat-ai-chat-setup` | 显式调用、独立确认；安装不执行 |
| 链接或本地文件转 MP3 | `media` / `wechat-ai-media` | Mac/Windows；非视频号不操作微信 |
| 整账号回放目录、音轨、完整转写与面板 | `replay` / `wechat-ai-replay-word` | Apple Silicon 预览；临时接入需单独批准 |
| 课程层级、音频优先、断点转写与合集 Word | `courses` / `wechat-ai-courses` | 授权本地清单；Apple Silicon MLX |
| 公开文章历史归档与本地控制台 | `articles` / `wechat-ai-articles` | macOS 可见会话；exact-biz、日期过滤 |
| 添加公众号 / 更新全部公众号到今天 | `articles` / `wechat-ai-subscriptions` | 增量去重、零新增不产空 Word |

[详细能力矩阵](docs/CAPABILITY_MAP.md) · [新机流程](docs/FIRST_RUN.md) · [课程清单](docs/COURSES.md) · [迁移与兼容](docs/MIGRATION.md) · [验证记录](docs/VALIDATION.md)

## 隐私与隔离

各服务运行在独立环境。新套件不读取旧项目数据库、不复用旧批任务、不覆盖旧 Skill。
媒体、课程与公众号数据默认位于当前用户 `WeChatAIServices` 应用支持目录；聊天使用独立的 `WeChatAIServicesChat` 私有目录。
模型、数据库、账号目录、签名 URL、媒体、Word、测试及真实任务证据均不公开。

默认关闭公众号调度与自动会话准备；旧后台安装器保留为独立兼容能力，需按迁移说明单独审阅。媒体旧第三方适配器存在于源码中，但统一入口不自动选用收费或需 token 的服务。

源码不等于成品验收。环境就绪不代表登录就绪；完整机器转写不代表逐字人工校听；Word 正文对账不代表已经渲染并逐页检查。

## 来源与许可

固定版本与改动见 [来源记录](docs/SOURCES.md) 和 `sources.lock.json`。公众号、媒体保留各自 MIT 许可；聊天来源为 UNLICENSED，本套件不擅自授予整体商用/再分发许可。依赖包括不同许可组件，详见 [许可边界](LICENSE.md)。
