# 许可范围

本整合仓库中 `delu543` 有权授权的源码、Skills 和文档采用根目录 [MIT 许可](../LICENSE.md)。这包括本仓库新增的统一入口、服务编排、课程适配，以及复制并适配到 `components/chat` 的聊天和回放源码。独立安装 `skills/` 下某个 Skill 时，该 Skill 目录也附有相同的 MIT 许可正文。

`components/articles` 与 `components/media` 保留各自来源的 MIT 许可和版权声明。`components/chat/LICENSE` 标明本整合仓库内聊天/回放源码的授权；其独立[来源仓库](https://github.com/delu543/wechat-data-extraction)是否另行授权，以来源仓库自身文件为准。本仓库的授权不自动修改其他仓库。

第三方 Python 包、FFmpeg、SILK、运行时二进制、可选下载资源和模型权重不因本仓库采用 MIT 而改为 MIT。安装或再分发时须遵守各自许可及署名、源码等义务，详见 [聊天依赖声明](../components/chat/THIRD_PARTY_NOTICES.md)和[媒体依赖声明](../components/media/THIRD_PARTY_NOTICES.md)。其中 macOS 聊天链路使用的 `pilk` 为 GPL-3.0；本仓库的 Git 源码树不捆绑其包或 FFmpeg 二进制。来源项目可能另行发布包含运行时的 Release Asset，须按其内容单独审查。若将依赖与本项目组合、打包或再分发，应核查对应 GPL 与其他第三方义务。MIT 授权本身不代表任何组合发行已经完成许可审查。

本许可不授予用户聊天数据、公众号文章、直播媒体或其他素材的权利，也不授予微信等第三方商标或平台访问权限。仅处理自己有权访问和使用的内容；操作范围及验收状态见 [首次使用](FIRST_RUN.md)和[能力矩阵](CAPABILITY_MAP.md)。
