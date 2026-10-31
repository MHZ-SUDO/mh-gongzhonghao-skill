# mh公众号skill

把中文讲解视频的制作与微信公众号发表接成一条流程：原片核对、语义精剪、系列封面、字幕、含图片与原生OMML公式的配套Word，以及公众号视频/图文草稿和发表。

可以只做其中一项，也可以“制作后发表”。视频与图文分别记录提交状态；正常新会话由用户完成登录、视频发表、图文发表所需的扫码和手机确认。实际验证次数以当前页面为准。

## 安装

将本仓库复制到 Codex 的技能目录，文件夹名保持 `mh-gongzhonghao-skill`，确保 `SKILL.md` 直接位于该文件夹内。然后在新会话中使用。

```powershell
# 已登录且有本私有仓库访问权限时：
gh repo clone MHZ-SUDO/mh-gongzhonghao-skill
# 将克隆得到的目录放入你的 CODEX_HOME/skills/。
```

界面名称为 **mh公众号skill**，英文调用名为 `$mh-gongzhonghao-skill`。也可直接用中文提出任务，由 Codex 自动匹配。

```text
用mh公众号skill整理这期讲解视频，封面、字幕和Word沿用系列风格。
已有终版，按之前的流程发布本期视频和配套文章。
做好本期视频和配套Word并发表；只让我完成必要扫码。
只做图文草稿，先不要发表。
```

## 环境与资源

- Python 3.10+；FFmpeg/ffprobe用于媒体处理，运行前验证其在当前PATH。
- 按功能需要安装 `requirements.txt`；离线ASR模型需单独准备，不随仓库分发。
- Windows Word COM渲染需要Microsoft Word；也可按当前documents技能选择其他可靠渲染方式。
- 封面可用imagegen；公众号操作使用已安装、已连接的kimi-webbridge和真实浏览器会话。

账号、作者、合集、工作目录及工具路径放在 `references/local-profile.md`，可从 [本地配置示例](references/local-profile.example.md) 开始。该文件被Git忽略。新机器需填自己的信息；共享示例不能作为真实发表授权。

入口是 [SKILL.md](SKILL.md)，制作与发布细节按需读取 `references/`。制作保留一个权威时间线、封面和事实版本；发布接收实际验收终版，不会上传原片或发布操作录屏。

## 脚本能力

| 文件 | 用途 |
| --- | --- |
| `scripts/media_tools.py` | 清点、SHA-256、帧时间映射、字幕边界和媒体/DOCX检查 |
| `scripts/omml.py` | 组合真正可编辑的Office Math公式，不是任意LaTeX转换器 |
| `scripts/render_word.py` | 独立隐藏Word实例渲染，保留用户已打开的Word |
| `scripts/publish_workflow.py` | 冻结素材与逐类型提交状态，防止超时后重复提交 |
| `scripts/test_publish_workflow.py` | 独立临时夹具验证发布状态机制，不发表真实内容 |

脚本不会自动判断剪辑语义、生成成片、操作网页或代替扫码。实际任务仍需由具备对应工具的助手执行并核验结果。

草稿转正式发表、未提交内容修改或本次类型范围变化时，使用 `refresh --state ... --config ... --out <新的预检目录>` 保留原记录，不删state重开。已提交类型继续锁定，未提交类型重新核验。无合集/无原创作者可用JSON `null`；只发一种类型不会检查另一种素材。上传阈值可用 `upload_limits` 按当前后台说明配置，历史默认值不代表当前平台规则。

运行本地发布回归检查：

```powershell
python -X utf8 -m unittest discover -s scripts -p test_publish_workflow.py -v
```

本仓库由原 `mh-video-word` 与 `wechat-lecture-publish` 合并而来，保留两部分可复用脚本、系列样图及历史案例。历史发表状态只代表当时观察，不是今天的线上状态。

只制作、安装、检查或演练不包含真实发表；明确要求发表时才进入对应流程。真实原片、成片、发布记录、登录信息和验证码应存放在项目目录，不提交到本仓库。
