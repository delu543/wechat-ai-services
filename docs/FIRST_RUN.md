# 新电脑首次使用

## 1. 先判断系统和目标

普通用户发送 README 中的首条提示即可，由 Codex 代办确定性步骤。
开发者运行 `python3 wechat_ai.py doctor all`。doctor 不安装、不初始化账号、不改网络；只报告本地环境。

- macOS：标准 CPython 3.10–3.13。聊天需要 macOS 15+ 和 Swift Command Line Tools。
- 直播/课程：Apple Silicon、CPython 3.12/3.13；模型本地路径另行指定。
- Windows：原生 PowerShell、本地 Codex；聊天需 CPython 3.12 x64、VC++ 运行库，媒体可用 3.10–3.13。不要用 WSL 冒充 Windows。
- 缺少 Python/VC++/Command Line Tools 时，由 Codex 给出对应系统安装步骤并说明下载；不要降低系统安全策略。套件目前不自带 Python。
- 已有媒体原仓库的 Windows 便携包可独立使用；它是原媒体产品的安装物，不是整套新服务的安装证据。

## 2. 只安装所需能力

```text
python3 wechat_ai.py install chat
python3 wechat_ai.py install media
python3 wechat_ai.py install articles
python3 wechat_ai.py install replay
```

课程复用 replay 环境。安装器按服务隔离依赖，不动旧项目，不安装同名旧 Skill，不开启调度。
聊天/媒体保留固定版本与发行哈希；公众号依赖仍有版本范围，直播传递依赖未全量锁定，不能称可复现发行。
安装不需要登录微信。新机环境完成后，再按实际目标要求本人登录。

## 3. 按目标继续

| 目标 | 下一步 | 本人需要做的事 |
|---|---|---|
| 聊天 | `run chat -- doctor`，随后 scan/export | 登录；首次显式调用 setup，分别确认初始化与快照；指定聊天、绝对时间、类型 |
| 本地文件转 MP3 | `run media -- convert-file <文件>` | 确认自己有权处理该文件 |
| 公共链接转 MP3 | `run media -- run <链接>` | 视频号 Windows 按提示手动播放；Mac 自动发送需明确授权 |
| 回放 Word | `run replay -- configure --name … --seed … --model … --service …` | 账号/种子明确；临时网络接入逐项批准；登录后打开种子一次 |
| 课程 Word | `run courses -- plan <清单>`，随后 run | 核对课程层级；提供授权本地素材与模型 |
| 公众号 | `run articles -- web --host 127.0.0.1 --port 8876` | 添加时给一条公共文章链接和日期/全部历史；正常登录/验证码本人完成 |

`run` 后使用 `--` 分隔转发参数。完整命令用所选 Skill，不能把表格中的省略号直接执行。
聊天初始化以 `components/chat` 为模块工作目录，使用该组件的 `live_tools` 初始化路径；普通 chat 路由没有初始化命令。Windows 必须先完整阅读对应 Skill 的 Windows 契约。

公众号套件配置在首次实际命令时创建，调度与自动会话准备默认 false。要授权可见会话恢复，先让 Codex 明确本轮来源及动作，再仅调整该套件配置的 `prepare_wechat_session`；不能碰旧配置。

## 4. 验收和恢复

先做用户授权的一场/一段/一个账号真实冒烟。按数量、顺序、完整解码、正文和最终文件验证，再扩大批量。
本机没有的微信历史媒体不会由本工具恢复。网站拒绝访问时保留失败状态，不更换身份绕过。
复用已有任务必须验证身份和文件哈希；活动 worker 存在时不要再启动。账号状态与输出不跨电脑自动迁移。
首次安装失败保留环境和日志；修正明确原因后最多重试一次，不无限重装或删除用户文件。
