# 按目标选择 Skill

Skill 告诉 Codex 如何使用完整仓库；它本身不包含全部运行代码或 Python/Swift 依赖。第一次使用请先看 [首次使用](../docs/FIRST_RUN.md)。

| 目标 | Skill | 主要交付 |
| --- | --- | --- |
| 不确定该用哪项能力 | [总入口](wechat-ai-services/SKILL.md) | 识别目标并选择服务 |
| 导出聊天及选定语音 | [聊天导出](wechat-ai-chat/SKILL.md) | 消息、媒体与语音 MP4 |
| 首次建立当前聊天账号的数据访问 | [聊天初始化](wechat-ai-chat-setup/SKILL.md) | 私有账号绑定；仅显式调用 |
| 链接或本地媒体转音频 | [媒体转 MP3](wechat-ai-media/SKILL.md) | 经验证的 MP3 |
| 整理账号直播回放 | [直播转 Word](wechat-ai-replay-word/SKILL.md) | 一份完整账号 Word 与进度面板 |
| 本地课程音视频转录 | [课程合集](wechat-ai-courses/SKILL.md) | 每合集完整 Word |
| 归档公众号可见文章 | [公众号文章](wechat-ai-articles/SKILL.md) | 本地文章档案与 Word |
| 添加公众号或更新所有已订阅账号 | [公众号更新](wechat-ai-subscriptions/SKILL.md) | 增量归档与新增 Word |

每个能力分别处理账号、素材与网络权限。选中 Skill 后先运行该服务的 `doctor`，只安装所需依赖。
