# 项目文件与通用工具

## 能力范围与运行

`scripts/media_tools.py` 只用 Python 标准库；媒体探测/解码另需 `ffprobe`/`ffmpeg` 在当前进程 PATH。它只读源文件，不删除、回收、改名、转写或生成成片。编译结果是给**项目渲染脚本**使用的基础时间线，不等于视频已经剪好。基础成片完成后用 [finalize_video.py](../scripts/finalize_video.py) 做一次整体1.25倍，详细命令与最终映射见 [final-speed.md](final-speed.md)。

本机示例先指定解释器与技能路径（其他机器自行替换）：

```powershell
$lecturePython = '<本机已验证的Python绝对路径>'
$lectureSkill = '<本机技能目录>\mh-gongzhonghao-skill'
& $lecturePython -X utf8 "$lectureSkill\scripts\media_tools.py" --help
```

所有报告使用新文件路径，已存在会拒绝覆盖。时间线/Word渲染使用新建或空目录，避免旧结果混入验收。文件读取统一 UTF-8，支持 BOM。命令使用参数列表执行，路径作为数据，不将文件名拼进 shell 命令。

## 1. 只读清点

```powershell
& $lecturePython -X utf8 "$lectureSkill\scripts\media_tools.py" inventory '<素材目录绝对路径>' --out '<成片目录绝对路径>\work\inventory.json'
```

默认只扫描该目录顶层的视频、常见音频、DOCX、SRT；子目录仅显式 `--recursive` 才扫描。不要对混有多期成片的总目录递归后直接据其删除文件。支持的扩展名见脚本开头 `VIDEO`/`OTHER`，可按实际格式适配。

报告每个文件的绝对路径、大小、修改时间、SHA-256及可用的 ffprobe 结果；哈希期间文件变化会报错。`exact_duplicate_groups` 只报告字节重复组，不选保留文件。探测失败记 `probe_error`，不伪装成可剪素材。

文件整理在项目中单独保存 `file-actions.json`，建议字段：`original_path`、`retained_path`、`renamed_path`、`sha256`、`action`、`authorization_basis`、`result`。使用 Windows 原生回收能力按文件逐个操作；执行前核验绝对路径和哈希。不要把目录递归删除当去重。

## 2. 人工已核对的剪辑决定

创建 UTF-8 `project.json`。下列是**演示数据**，需要替换真实路径、主题和区间。例子是顺序播放A后B，不代表只有两段：

```json
{
  "schema": "mh-video-project/v1",
  "title": "本期实际主题",
  "fps": "30/1",
  "sample_rate": 48000,
  "cover_frames": 60,
  "sources": [
    {
      "id": "a",
      "path": "../第NN期_01_主题_原片.mp4",
      "spans": [
        {"start": 0, "end": 1, "speed": 0, "reason": "确认无讲话的片头等待"},
        {"start": 1, "end": 8, "speed": 1, "reason": "定义与关键讲解"},
        {"start": 8, "end": 12, "speed": 2, "reason": "确认无讲话的书写"},
        {"start": 12, "end": "end", "speed": 1, "reason": "算例及总结"}
      ]
    },
    {
      "id": "b",
      "path": "../第NN期_02_主题_原片.mp4",
      "spans": [
        {"start": 0, "end": 2, "speed": 0, "reason": "已核对的重复引入"},
        {"start": 2, "end": "end", "speed": 1, "reason": "后续推导"}
      ]
    }
  ]
}
```

约束：

- 路径相对于 `project.json` 所在目录；`id` 唯一非空，所有源文件不同且不含字节重复。推荐每个源加 `sha256` 字段，从清点报告取得，编译会检查匹配。
- `sources` 数组就是已经核对的合并顺序；每源 `spans` 按源时间顺序，完整分割源视频。连删除部分也必须列出，没有空洞/交叠，不允许把尾段漏掉当作完成。
- 所有区间半开 `[start,end)`，数值以源共同时间零点的秒表示。最后用字符串 `"end"` 避免输入显示时长四舍五入造成越界。开头必须0，每段必须有原因。
- `speed` 仅整数0、1、2，属于基础精剪层：0删除，1原速，2已确认的无解说加速。最终整体1.25倍是基础成片之后的单独步骤，不把1.25写进此分段字段。其他局部分段速度、同一源跨段重排或交叉淡化须扩展渲染器/格式，不能假称compile已经支持。
- `fps` 为精确有理数字符串（例如 `"30000/1001"`）；不能用29.97冒充其精确值。`sample_rate` 正整数，`cover_frames` 非负整数。封面0帧表示本次无片头；PNG存在不等于自动渲染了片头。

```powershell
& $lecturePython -X utf8 "$lectureSkill\scripts\media_tools.py" compile '<成片目录绝对路径>\project.json' --output-dir '<成片目录绝对路径>\work\timeline-v1'
```

### 帧与样本规则

工具优先读取精确 `video.duration_ts × time_base`，缺失才用视频小数时长/容器时长，并记录 `duration_basis`；避免29.97类帧率的小数截位造成少算末帧。以 `floor(duration × project_fps)` 定义规整后的可用源帧数。切点最近帧取整，恰半帧向上；最后不足一帧的尾部记录在源时长中，不伪装成已保留。小数时长退路需核实末帧精度；源最后一帧若仍有讲话，应调整渲染策略而非截断句子。

先按项目 fps 规整源，再选择行 `[a,b)`：原速输出 `b-a` 帧；2倍速从行首抽每第二帧，输出 `ceil((b-a)/2)` 帧。**渲染器必须使用相同取样相位**，最终用实际帧数核对，不把理想倍速秒数另作权威。

输出 `timeline.json`、`edits.csv`、`statistics.json`。每行含源/输出帧区间、速度、原因、输出秒及累计输出样本位置。音频片段样本数为 `round(output_end_frame*rate/fps) - round(output_start_frame*rate/fps)`；因此非整除帧率不会逐段积累误差。封面样本在 `cover_samples`，需由渲染器补相同时长。

工具编译的是从0计时的源。若 ffprobe 显示视频/音频 `start_time` 不同，先在项目提取/规整时定义共同0点，保留所需前导空隙并验证同步；本工具不会替你校正 PTS。也不会自动估算旋转、非方形像素或VFR画面选择。

`original_seconds` 是去重后不同源时长之和，`final_seconds` 含实际配置的封面。缩短比例可为负（片头可能长于删除内容），不强制裁成“缩短”。视频渲染后核对最终帧数、时长，而不是只引用编译统计。

## 3. 词级边界检查与重映射

把 ASR/校对层转换为平铺列表 `words.json`：

```json
[
  {"source":"a","start":1.2,"end":1.6,"text":"步长"},
  {"source":"a","start":7.9,"end":8.1,"text":"速度"},
  {"source":"b","start":0.3,"end":0.5,"text":"嗯","allow_removed":true}
]
```

```powershell
& $lecturePython -X utf8 "$lectureSkill\scripts\media_tools.py" map-words '<成片目录绝对路径>\work\timeline-v1\timeline.json' '<成片目录绝对路径>\work\words.json' --out '<成片目录绝对路径>\work\mapped-words-v1.json'
```

词与被删区间相交、词跨编辑边界、词在加速区或不足一帧尾部时，写 `review_required`。`allow_removed` 仅在已人工确认要删的词上使用，不是抹掉剪辑问题的通用开关。词内有切口须回听修改；输出中的拆分词保留相同文本是供检查，不可直接拼成字幕导致重复。

依照回听和时间映射重新分句校对，生成最终字幕对象，再从同一对象生成 ASS/SRT。脚本不自动断句，不进行中文术语纠正，不生成带语义保证的字幕。

## 4. 独立事实/勘误层

项目可用 `facts.json` 保存：期次/主题来源、坐标与变量、每个独立例题参数、公式与适用条件、逐项复算、勘误决策及依据。每条勘误应有 `source`、源区间、`original`、`corrected`、`basis`、`downstream_affected`、最终处理和核对状态。

这些不是 `compile` 自动执行的字段；项目渲染脚本将勘误区间与每个保留行求交，再按同一映射生成独立标注轨。跨剪口也要覆盖，不能仅靠字幕中点判断。其他例题开始时撤下前例标注。

## 5. 基础成片与最终倍速成片核验

下面的命令对照基础编译timeline，仅用于整体倍速之前的基础精剪版。最终1.25倍MP4必须改用其 `.finalize.json` 作 `--timeline`，并配最终缩时SRT，完整示例见 [final-speed.md](final-speed.md)。

```powershell
& $lecturePython -X utf8 "$lectureSkill\scripts\media_tools.py" verify-media '<成片目录绝对路径>\终版.mp4' --out '<成片目录绝对路径>\qa\media-v1.json' --timeline '<成片目录绝对路径>\work\timeline-v1\timeline.json' --srt '<成片目录绝对路径>\终版.srt' --width 3200 --height 2000 --fps '30/1' --video-codec h264 --audio-codec aac --full-decode
```

尺寸/帧率/编码参数按当前项目替换。工具要求视频及音频流，检查总体时长差，带 `--timeline` 检查预期时长，带 `--srt` 检查正时长、顺序、无重叠/越界，带 `--full-decode` 解码完整视频及音轨并留日志。失败退出非0并在可写情况下保留报告。

未带全片解码时状态仅 `partial_technical_checks_passed`；完整成功仍只叫 `technical_checks_passed`。报告中的真实图像、听感检查固定为未由工具执行。可变帧率/流起始差、中途漂移、公式清晰、语义、响度和实际烧录字幕一致性需另做验证。该工具不会凭片长匹配就保证同步。

## 6. Word 原生公式与封面核验

```powershell
& $lecturePython -X utf8 "$lectureSkill\scripts\media_tools.py" inspect-docx '<成片目录绝对路径>\配套.docx' --cover '<成片目录绝对路径>\封面.png' --min-math 1 --out '<成片目录绝对路径>\qa\docx-structure-v1.json'
& $lecturePython -X utf8 "$lectureSkill\scripts\render_word.py" '<成片目录绝对路径>\配套.docx' --output-dir '<成片目录绝对路径>\qa\word-render-v1' --dpi 180
```

`--min-math` 按本期必要公式数量选择，不强制第三期的数量；没有可编辑公式要求时可不填。`inspect-docx` 检查 ZIP/正文XML、原生数学块与部分结构数量、嵌入媒体哈希，比较封面是否原样嵌入；它不证明数学正确、全部公式都正确或封面确实排版在可见页中。可疑原始 LaTeX 只作人工检查提示。

`render_word.py` 需 Windows、Microsoft Word、pywin32、lxml、PyMuPDF。它使用专用隐藏只读实例，禁宏，核对新进程归属，只关闭自己创建的文档/实例，导出 QA PDF+页面 PNG+meta。不要同时启动多个此类渲染，避免进程归属歧义；不要在错误后杀所有 WINWORD。新/空目录要求在启动 Word 前检查。

渲染成功仍需要逐页查看；页数、结构数量和哈希都是证据的一部分，不能代替视觉与科学检查。文档生成和 OMML 示例用法详见 [cover-and-word.md](cover-and-word.md)。
