# 能力与平台矩阵

| 来源 / 能力 | 状态 | 套件执行路径 | 验收边界 |
|---|---|---|---|
| 聊天解析、精确范围、媒体关联与原子归档 | active | chat doctor/scan/export | 合成回归；旧 Mac 实机证据不等于新机完成 |
| SILK 顺序合并、完整解码、严格 MP4 | active | chat direct-voice-mp4 | Swift/媒体自检；Windows 实机待验收 |
| 当前账号绑定、快照、显式初始化 | active | 独立 setup Skill | 不在本轮读取账号或初始化 |
| 链接分类、公共网页/媒体下载与 MP3 | active | media run/convert-file/verify | 本地真实转码；外站逐平台另验收 |
| Windows 手动播放接力 | experimental | media --manual-playback | 源码保留；微信构建/运行目录需实机 |
| 视频号目录分页、临时接入、回滚 | experimental | replay prepare/capture | Apple Silicon 原型；本轮未改 CA/代理 |
| Range 音轨、时长证据、重试熔断 | active | replay batch | 本机回归；真实 CDN/账号需授权 |
| 时间轴保真 ASR、缓存指针、已验音轨恢复 | experimental | replay transcribe/batch | schema 2；额外一次仅限明确授权序号；真实 MLX 待验收 |
| 逐场 Word 与整账号连续章节 Word | experimental | replay word/assemble | 冻结目录、当前转写与已验 Word 对账；最终单文件另渲染 |
| 只读实时面板 | active | replay dashboard | 回环随机路径、并发读加锁；区分逐场待验/已验与最终交付 |
| 课程层级、音频优先、合集完整性 | experimental | courses plan/run | 显式本地清单；不携带课程网站凭据 |
| 课程平台数据层及特定版本映射 | active 原项目 / 未通用发布 | 独立迁移边界 | 账号绑定脚本原处保留，非新机支持承诺 |
| 公众号精确 biz、日期、分页、正文解析 | active | articles reverse/history-sync/crawl | 可见会话依赖；受限来源暂停 |
| 公众号 Markdown、增量 Word、去重 | active | articles export/automation-run | 新空库回归，不迁移旧库 |
| 添加公众号 / 更新全部到今天 | active | 独立 subscriptions Skill | 保存种子；从 offset 0 增量扫描 |
| 暂停/恢复、周期记录、本地 UI | active | articles web | 默认关闭调度/自动会话准备 |
| 公众号后台/菜单栏 | active 兼容源码 | 独立显式部署 | 新身份 Swift 编译与真实权限验收分开 |
| 自动临时文件/原文清理 | changed | 统一入口预览 | 真实删除需单独批准 |

Mac 聊天、链接媒体与公众号为不同能力；不得把聊天 Windows 测试扩展为直播/公众号 Windows 支持。
Linux/云端不作为本机微信运行环境。Windows 需要原生本地执行。

没有移除原项目文件或能力。新套件刻意不自动读取旧账号/任务，不自动启动后台服务；保留独立兼容路径与明确迁移限制。
