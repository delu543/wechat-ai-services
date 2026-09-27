# Windows 10/11 + 微信 4.x：源码预览

目标与 Mac 相同：在 Codex 中说出聊天名、绝对时间范围和内容类型，先扫描，再本地导出。
不是截图/OCR，也不要求用户找数据库路径。Windows 不需要 Swift、Frida 或 Mac 权限。

## 用户入口

**新用户先看 [Windows 新手指南：第一句话、本人操作与 Codex 回复](WINDOWS_FIRST_RUN.md)。**
安装阶段不需要先登录微信；到账号初始化/导出时，本人在官方微信登录并打开任意聊天，
保持打开即可。已登录不用重登，不要求截图、数据库路径或逐条播放。
Codex 只提示当前需要的一步，跳过已有的就绪状态；初始化与明文快照保留仍单独确认。

1. 在 Windows Codex 中克隆并打开完整 GitHub 仓库，说“帮我安装并检查微信数据提取项目”。
2. 首次账号初始化，用户显式输入 `$wechat-local-export-setup`。
3. 分别确认依赖安装、当前账号的一次只读内存初始化、私有明文快照保留。
4. 此后直接说“导出某群昨天 9 点到 18 点的文字、图片和语音”等；有歧义或缺失会停止。

手动入口（在仓库根目录的 PowerShell 中）：

```powershell
.\scripts\codex_bootstrap.ps1 doctor
.\scripts\codex_bootstrap.ps1 install
```

要求 Windows 10/11 **x64**、官方微信 4.x、CPython **3.12 x64**。缺少 Python 时，Codex
应说明下载来源并得到安装同意，再用官方 Python 安装包或 `winget` 的
`Python.Python.3.12` 包；不能偷偷修改系统执行策略、关闭杀毒或提权。需要可加载 SILK
扩展的 Microsoft Visual C++ 运行库；缺失时安装会停止并提示，不能假装准备完成。

安装器只下载固定版本、固定 SHA-256 的 Windows wheels，不从源码编译 C++。安装路径由
Windows Known Folder API 取得；完整源码、运行环境、两个 Skill 均安装在当前用户范围。
同版本内容幂等；遇到外来的同名 Skill 会保留并停止。升级保留旧源码和旧 Skill 副本，
不自动删除密钥、快照、历史任务或导出结果。Windows 暂未提供自动卸载命令。

## 实现与边界

| 环节 | Windows 实现 |
| --- | --- |
| 当前账号 | 验证微信可执行文件的有效腾讯 Authenticode 签名和主版本 4；同一 Windows 用户的进程持有准确 contact/message 文件，两次样本必须一致 |
| 初始化 | 独立显式入口；只读访问该进程，限 90 秒/1 GiB/512 个候选；逐目标 salt、SQLCipher 首页面 HMAC 和结构验证；不能完整匹配就停止 |
| 保存状态 | 当前用户 DPAPI 加密初始化状态；私有 ACL 仅允许当前用户和 SYSTEM；普通导出客户端没有捕获入口 |
| 最新快照 | Windows LockFileEx 协调 SQLite SHM 的 120–122 字节；复制主库/WAL，释放锁后重放已提交帧；无 SHM 的关闭分片用禁止写入/删除共享的只读句柄证明稳定 |
| 分片变化 | 刷新全部已初始化消息分片，防止聊天跨分片而遗漏；新增分片需重新明确扩展初始化，不会静默跳过 |
| 内容导出 | 复用 Mac 的聊天/时间/类型筛选、消息解析、附件关联、哈希验证和原子发布 |
| 语音 | Windows pysilk-mod 解码，FFmpeg 逐条 M4A 或严格流式 MP4；不使用 Swift |

在线协调锁的复制时间限制为 30 秒；数据库过大、持续忙碌、权限不足、路径为网络盘/
重解析点、多个当前账号、进程重启或格式变化时安全停止。不会强退微信、修改微信程序、
注入代码、发送消息、操作登录、绕过扫码，也不会转用远程服务。

图片/附件仍受本地缓存和格式限制。旧 XOR、V1、已有候选能够验证的 V2 图片走共享解码；
Windows 微信构建改变图片密钥来源时可能无法恢复，严格模式会报告并停止，不承诺全部图片。
视频仍仅导出元数据。Word 不是底层导出格式；可在用户另行要求时把已导出的文字归档转为 Word。

## 验证状态必须分开

- Mac 原有能力继续由 `scripts/release_check.sh` 回归。
- Windows 原生测试在 GitHub Actions 的 Windows runner 中执行，覆盖 ACL、DPAPI、真实
  SQLite 写锁、合成加密数据库/WAL 的最新提交、真实 SILK/FFmpeg 两种语音模式和管道超时。
- **没有 Windows 10/11 官方微信实机验收记录，不能宣称所有微信 4.x 构建可用。** Windows
  版本为源码预览；尤其是账号句柄可见性、腾讯签名变化、内存密钥布局、媒体缓存布局仍须
  用授权 Windows 机器按下列验收确认。CI 通过不是这些外部门槛的替代。
- SQLCipher 首页面 HMAC 在 Windows 初始化中验证；快照并未认证每一页，报告仍明确
  `page_hmac_verified: false`，不能称为完整认证解密。

实机验收：干净安装 → 当前账号明确初始化 → 单独确认保留快照 → 指定群/时间的文字扫描
与微信人工计数一致 → 收到新消息后重新扫描能包含新消息 → 图片/语音/文件完整归档及 MP4
逐条顺序、数量、时长验收 → 切换账号、无权限、缺媒体、忙锁均停止 → Mac 回归不退化。
记录 Windows/微信完整版本和匿名计数即可，不能把数据库、账号路径、密钥或聊天放进 CI。

## 技术来源与依赖审查

实现优先复用仓库已有解析器、SQLCipher 页面解密和 WAL 校验器，没有引入整套爬虫或下载
外部密钥工具。Windows 原生边界为本仓库实现，不使用未明确许可的第三方提取脚本代码。

- [SQLite Windows VFS](https://sqlite.org/src/file/src/os_win.c)：共享内存锁协议。
- [Microsoft LockFileEx](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-lockfileex)、
  [ReadProcessMemory](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-readprocessmemory)、
  [DPAPI](https://learn.microsoft.com/en-us/windows/win32/api/dpapi/nf-dpapi-cryptprotectdata)：原生接口。
- [SQLCipher 设计](https://www.zetetic.net/sqlcipher/design/)：AES-CBC、SHA-512 HMAC 与 KDF；
  [MSVC STL string 布局](https://github.com/microsoft/STL/blob/main/stl/inc/xstring)：候选观察依据，
  不代表微信承诺使用该布局。新构建可能不匹配。
- `pywin32 311`（PSF）、`psutil 7.0.0`（BSD-3-Clause）、`pysilk-mod 1.6.4`（上游 BSD/MIT
  及组件声明）、`zstandard 0.23.0`、`pycryptodome 3.23.0`、`imageio-ffmpeg 0.6.0`：使用
  CPython 3.12 Windows x64 wheels，版本/哈希在 `scripts/requirements-windows.txt`。
  pysilk-mod 最近固定版发布于 2024 年，维护和新 Python 兼容性存在风险，因此不浮动升级。
- [pysilk-mod API/源码](https://github.com/DCZYewen/Python-Silk-Module) 与
  [许可证](https://github.com/DCZYewen/Python-Silk-Module/blob/master/LICENSE) 已审阅；只调用
  解码/编码 API，测试不调用播放、异步线程池或退出函数。FFmpeg 是单独许可证组件。

公共仓库不包含 wheels、FFmpeg 二进制或私人状态；首次安装才从 PyPI 下载依赖。详情见
[第三方声明](../THIRD_PARTY_NOTICES.md)。商用重新打包二进制前需另行完成许可证审查。
