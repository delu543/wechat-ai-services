# 来源、复用与许可证审阅

完整固定版本见 `sources.lock.json`：

- [聊天与回放](https://github.com/delu543/wechat-data-extraction)：`a4f7525c50713935dcd145f45314036fe0fe590a`，保留解析、快照、媒体、初始化、回放核心。来源标识 UNLICENSED，未擅自改为开源许可。
- [转录媒体依赖](https://github.com/delu543/weixin-replay-to-mp3)：`4cec3daecf67d65ec10eb5f1ef0c86783efbe514`，MIT，保留第三方声明。是转录的依赖来源，不另扩为一个业务整合项目。
- [公众号发布基线](https://github.com/delu543/wechat-public-account-archiver)：`b2d0fc39fb52a93f482da5f6fcebee55e6c09dc9`，MIT；不是该仓库默认分支旧版本。另审阅采用业务工作区的功能增强和发布工作区的安全规则更新。
- 转录平台的本机课程实现：按职责提取清单/层级/音频优先/串行断点/完整文档，复用已通用化的 ASR。私人接口状态及内容没有复制。

整合前审阅了 [Skills 官方 CLI](https://github.com/vercel-labs/skills) 的按名称安装方式，以及 [uv 环境隔离文档](https://docs.astral.sh/uv/pip/environments/)。本版沿用已有 Python venv 与各服务依赖，避免为整合引入新的包管理运行前提；没有复制这两个项目源码。

适配范围：私有目录命名空间、统一入口、运行时 FFmpeg 路径、独立 Skills、公众号业务增强、课程清单与通用 Word 类型。未重写加密/下载/消息解析算法。
固定源版本有既有回归证据；上游持续维护状态、未来微信协议兼容性不由固定提交保证。
源码摘要清单用于安装/发布一致性审阅，不含本机绝对路径。依赖许可详见各组件 THIRD_PARTY_NOTICES、LICENSE 和根 LICENSE.md。
