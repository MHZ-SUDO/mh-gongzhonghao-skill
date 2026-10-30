---
name: mh-gongzhonghao-skill
description: mh公众号skill：精剪中文课程和技术讲解视频，制作系列封面、中文字幕与含原生公式的配套Word，并按本次授权完成微信公众号视频和图文的草稿或发表。适用于制作、发表、制作后发表及续接发布；普通视频播放、独立论文写作不触发全套流程。
---

# mh公众号skill

从原始讲解到公众号视频与配套文章，共用一套期次、事实、封面和终版素材。先识别本次范围，只运行需要的分支；已有合格终版直接续用。默认自动匹配，不要求用户记住英文调用名。

## 范围与入口

| 用户要求 | 执行范围 | 先读 |
| --- | --- | --- |
| 剪视频、封面、字幕、按视频做Word | 制作全部或指定部分，交付所需文件 | [制作流程](references/production-workflow.md) |
| 已有成片，存公众号草稿或预览 | 素材核验、后台编辑和保存，不发表 | [发布流程](references/publishing-workflow.md) |
| 发本期视频、图文或两者 | 直接续用终版，执行对应发表类型 | [发布流程](references/publishing-workflow.md) |
| 做好本期视频和配套文章并发表 | 制作验收后衔接本期发表 | 两个流程，先制作后发布 |
| 继续上次发表、刚才点发表超时 | 原state、稿件ID、原浏览器现场的恢复分支 | [检查与恢复](references/checks-and-recovery.md) |
| 合并/安装/测试skill、检查素材、演练 | 只做相应工作，不在公众号真实发表 | 按所需资源读取 |

用户明确要求发表且期次、对象清楚时，已授权完成该范围；做好可审阅预览后继续，不重复询问是否发表。只说“做公众号视频/文章”按制作处理；“做成skill”“存草稿”和历史录屏中的点击不构成发表授权。视频与图文分别按 `video_only`、`article_only`、`video_and_article` 处理，不增加内容类型。

若存在 `references/local-profile.md`，先读与本次相关的部分，沿用该用户已确认的系列、账号和环境信息；该文件可缺省，不属于共享包。没有本地配置时从当前项目和当次要求确定账号、作者、合集、文件及工具，不能把示例填入真实稿件。当前要求优先；必要且无法从上下文核实的歧义集中问清，同时推进独立工作。

## 始终保持的关系

- 原片先记大小和SHA-256，保留完整来源。哈希相同才是字节重复；清点不自动改名或删除，已授权去重仅核验后回收指定副本。
- 以讲懂为前提缩短：讲解原速，删重说与空白，书写结合画面判断。数字、公式、单位与算例先独立核对，勘误贯穿视频、字幕、封面和Word；不为迁就答案修改输入。
- 源帧、成片帧、音频样本和字幕共用最终时间线，片头计入；修改剪点后重建受影响输出。
- 本期最终PNG用于独立封面、视频片头和Word；用哈希核验版本。Word原生公式是真实OMML，公众号导入后仍需核对可读性。
- 技术检查与实际听看分开记录。解码不证明讲解连贯，OOXML检查不证明排版合格，不把未执行的验收写成已完成。
- 每次后台写操作读取当前页面、弹窗和唯一目标；超时先读状态。浏览器写操作顺序执行，不复用录屏坐标、旧元素引用或旧token。
- 视频和图文独立记录身份与提交。已发表/发表中先查再接管；同标题不同类型不当作重复稿。
- `commit`先落盘attempted，再点击一次最终发表确认；扫码过期、超时或pending不清state、不新建run绕过。手动或前会话已提交的记录用 `adopt` 接管；后续用 `record` 更新。只有真实回执证明已发表才记published。

## 从制作交接到发表

1. 在本期工作目录完成制作验收，确定实际MP4、PNG、SRT、DOCX及事实/勘误记录。现成终版只检查缺项，不为走流程重新剪辑。
2. 只有本次包含发表或草稿准备时进入后台分支。把**已验收终版的绝对路径**填入 [发布配置](references/example-config.json)，保存当次用户要求原文和对应类型。仅草稿时 `authorization.requested_action` 为 `prepare`；当次已明确要求发表才填 `publish`，不只修改原文而遗漏此字段。发布操作录屏只列入 `reference_recordings`，不能作为讲解视频。
3. 在技能目录外建立本期发布记录目录。新准备任务运行 `prepare → init` 冻结文件哈希；恢复attempted/pending任务先读原state与线上原稿，不重新初始化。终版在冻结后修改必须重新核验；若已经attempted，进入恢复分支，不重新提交。
4. 按发布流程检查账号、标题、封面、公式、合集、原创归属和当前页面。默认群发通知OFF、定时OFF；本地既定设置只适用于已确认的该用户系列。
5. 仅存草稿时保存并核对状态后交付。真实发表时每种类型分别 `review → commit → 一次网页提交 → record`，发表前读 [检查与恢复](references/checks-and-recovery.md)。
6. 正常全流程的扫码阶段是登录、视频发表、图文发表。已有登录或已完成某种类型时跳过对应阶段；显示当前有效码、等待手机确认并读取实际回执，不把“三处”当必须凑足的次数。

`publish_workflow.py`只冻结本地输入和记录提交，不操作网页、不替用户扫码；`media_tools.py`只清点、编译时间映射与技术核验，不自动转写、语义剪辑或渲染。两者的成功输出不等于视频已经制成或公众号已经发表。

## 按需读取

| 需要 | 资源 |
| --- | --- |
| 制作步骤与局部任务 | [production-workflow.md](references/production-workflow.md) |
| 系列风格、封面样图与格式 | [series-profile.md](references/series-profile.md)，使用前查看所引用样图 |
| 转写、自然删剪、音频、算例与验收 | [video-editing.md](references/video-editing.md) |
| 封面、原创图文、图解与OMML | [cover-and-word.md](references/cover-and-word.md) |
| 媒体脚本、时间线JSON与命令 | [project-format.md](references/project-format.md) |
| 公众号操作、扫码、文档导入与预览 | [publishing-workflow.md](references/publishing-workflow.md)，[publishing-runbook.md](references/publishing-runbook.md) |
| 发布检查、状态命令与异常恢复 | [checks-and-recovery.md](references/checks-and-recovery.md) |
| 平台实录、默认设置的适用范围 | [profile-and-evidence.md](references/profile-and-evidence.md) |
| 历史纠错经验，避免照搬第三期参数 | [episode-03-case.md](references/episode-03-case.md) |

制作需要的封面生成与Word排版使用当前可用的imagegen、documents技能；真实后台操作使用已安装的kimi-webbridge并遵循其健康检查和会话规则。先验证本机工具，不把示例机器路径当固定安装位置；工具缺项时说明实际缺口，不假称操作成功。

## 交付

只制作时链接本次要求的MP4、PNG、SRT、DOCX，简报时长变化、重要纠错和实际检查范围。发表时分别报告视频/图文的标题、类型、已发表/处理中/失败、可用链接和发布记录目录。仍在扫码或提交受理未知时保持原验证页；平台已受理且仅剩后台处理时保存证据，可结束操作并按浏览器技能清理会话。

## 调用示例

- “用mh公众号skill精剪这期，封面和Word沿用上次风格。”→ 制作，不发表。
- “已有终版，发本期视频和配套图文，按之前的流程来。”→ 只核验与发表。
- “把这期视频、封面、字幕、Word做好并发表。”→ 制作验收后连续发表。
- “只做封面”“只发视频”“存图文草稿”→ 只执行指定部分。
- “刚点发表超时了，继续处理。”→ 恢复原记录，不重发。
