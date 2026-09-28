# 功能源码

这里保留已审阅的原功能与其契约，供维护和溯源。普通使用从根目录 [README](../README.md) 和 [wechat_ai.py](../wechat_ai.py) 进入；组件内旧安装器不承担整合套件的安装与数据路径管理。

| 目录 | 负责的能力 | 套件入口 |
| --- | --- | --- |
| [chat](chat/README.md) | 聊天解析、精确导出、媒体与语音处理；含直播回放组件 | `chat`、`replay` |
| [articles](articles/README.md) | 公众号文章发现、归档、增量更新与本地控制台 | `articles` |
| [media](media/README.md) | 链接/文件识别、音频获取、转码与验证 | `media` |

课程合集编排位于 [services/courses.py](../services/courses.py)，复用直播的本地 ASR 与 Word 构建能力。来源提交、许可及适配边界见 [来源记录](../docs/SOURCES.md)。
