# umamusume-anime

> 一个本地优先、素材驱动的轻量剧情演绎视频生成项目。  
> 输入小说 / 剧本草案、角色 PNG、角色参考音频、场景图或场景提示词，输出带双语字幕、旁白和简单角色演出的 MP4 视频。

---

## 1. 项目目标

这个项目要解决的不是“做一个完整动画编辑器”，而是先把一条**稳定、可复用、可批量化**的流水线跑通：

**文本 -> 角色台词 -> TTS 音频 -> 时间轴 -> 视频渲染**

第一阶段只做“轻演出”版本：

- 输入小说或剧本草案
- 按角色拆出台词和旁白
- 为每个场景绑定背景图；如果没有背景图，则先产出场景生图 prompt
- 按句调用 TTS 生成音频
- 根据音频时长自动生成时间轴
- 用角色 PNG + 场景图 + 字幕 + 音频合成视频
- 谁说话，谁的立绘做轻微跳动 / 缩放 / 高亮
- 输出双语字幕（如：日语台词 + 中文字幕）
- 支持旁白

### 当前明确**不做**的内容

为了尽快做出可用版本，以下内容暂时不在 MVP 范围内：

- 嘴型同步
- 复杂镜头语言（推拉摇移、多机位）
- Live2D / 3D 动画
- 可视化时间线编辑器
- 商业化发行支持

---

## 2. 这个项目最终长什么样

目标是把一段小说 / 剧本草案，自动整理成一个可渲染的视频项目：

1. 读入原始文本
2. 把文本拆成场景、对白、旁白、情绪标签
3. 给每句对白选一个说话角色
4. 给每个场景绑定背景图；若缺图则生成场景 prompt
5. 逐句调用 TTS，生成音频文件
6. 读取音频时长，生成 `timeline.json`
7. 交给 Remotion 渲染为 MP4

一句话概括：

> **这是一个“赛马娘小说 / 剧本 -> 轻演出视频”的本地流水线，而不是传统 NLE 视频剪辑工程。**

---

## 3. 用什么实现

### 3.1 视频编排与渲染：Remotion

Remotion 负责最终视频合成。它的职责包括：

- 读取背景图、角色 PNG、音频、字幕、时间轴
- 在时间轴上控制角色出现、消失、切场景
- 对当前说话角色做轻微动画
- 输出最终 MP4

它非常适合这个项目，因为这个项目本质上是“**数据驱动的视频生成**”，而不是手动拖时间线。

### 3.2 日语角色音色 / 日语对白：Qwen3-TTS

Qwen3-TTS 主要负责：

- 日语角色音色克隆
- 日语对白合成
- 需要时生成稳定的角色声线 prompt 缓存
- 旁白声线设计（可选）

本项目中的默认策略：

- **克隆日语音色，说日语**：优先走 Qwen3-TTS

### 3.3 跨语种中文演绎 / 情绪表达：IndexTTS2

IndexTTS2 主要负责：

- 用日语参考音色克隆角色后说中文
- 更灵活的情绪控制
- 当需要中文演绎版本时，生成更合适的中文对白音频

本项目中的默认策略：

- **克隆日语音色，说中文**：优先走 IndexTTS2

### 3.4 素材获取：umamusume-voice-data

用于获取：

- 角色语音素材
- 角色立绘素材

它不是视频生成器，而是素材准备工具。

### 3.5 编排层：Python + TypeScript

- **Python**：作为总调度与 TTS 封装层，适合接模型、音频处理、批量任务
- **TypeScript / Node.js**：作为 Remotion 视频工程与渲染 CLI

这样拆开后，职责比较清晰：

- 模型和音频逻辑留在 Python
- 视频与前端渲染逻辑留在 TS / React

---

## 4. 当前仓库的组织方式

当前仓库已经有一个很接近目标形态的雏形：

- 根目录有 Python 入口
- `my_tts/` 负责 TTS 相关逻辑
- `my-video/` 是 Remotion 视频工程
- `my-video/cli/` 已经有 CLI / service / timeline 相关代码
- `my-video/src/components/` 已经有视频组件雏形
- `my-video/public/content/...` 已经有一套内容目录示例

也就是说，项目现在并不是从零开始，而是已经具备：

- 一个 Python 侧入口
- 一个 TTS 模块
- 一个 Remotion 原型工程
- 一个按内容目录组织素材的初版结构

这非常适合继续往“统一流水线”推进。

---

## 5. 当前结构下的职责划分

建议保持现有拆分，但把边界写清楚。

### 根目录 `main.py`

作为总入口，负责串联整个流程：

- 解析剧本
- 规划场景
- 调用 TTS
- 生成时间轴
- 调用视频渲染

它不直接做模型推理，也不直接写 React 组件，只负责编排。

### `my_tts/`

作为统一 TTS 入口，负责：

- 对接 Qwen3-TTS
- 对接 IndexTTS2
- 提供统一的命令行接口
- 处理角色参考音频、情绪参数、缓存
- 输出逐句音频文件

这个模块的目标不是“自己训练模型”，而是把多个 TTS 引擎包装成统一接口。

### `my-video/`

作为视频渲染工程，负责：

- 读取时间轴 JSON
- 渲染背景、角色、字幕、旁白
- 执行说话角色的小幅动画
- 输出视频

它只关心“如何渲染”，不关心“音频是怎么合成出来的”。

---

## 6. 推荐的数据流

项目推荐采用下面这条确定性的流水线：

```text
novel.md / script.md
  -> script.json
  -> scene-plan.json
  -> audio/*.wav
  -> timeline.json
  -> final.mp4
```

### 每一步的作用

#### 1) 原始文本 -> `script.json`

把小说 / 剧本解析成结构化数据：

- scene
- dialogue
- narration
- speaker
- spoken text
- subtitle text
- emotion
- scene id

#### 2) `script.json` -> `scene-plan.json`

如果当前场景没有准备背景图，则产出：

- 场景描述
- 生图 prompt
- 建议镜头氛围
- 场景持续时长估计

如果已经有背景图，这一步只做绑定。

#### 3) `script.json` -> `audio/*.wav`

逐句调用 TTS：

- 给每句对白选用哪个引擎
- 生成每句对应音频
- 为旁白生成单独音频
- 记录路径、采样率、时长、缓存 key

#### 4) `audio/*.wav` -> `timeline.json`

根据音频时长自动推导：

- 每句开始时间
- 每句结束时间
- 当前说话角色
- 字幕显示区间
- 场景切换点

#### 5) `timeline.json` -> `final.mp4`

Remotion 读取时间轴并完成渲染。

---

## 7. 关键数据文件

为了让 Python 与 Remotion 之间解耦，建议把数据契约固定下来。

### 7.1 `speaker.yaml`

每个角色一个配置文件，记录：

- 角色 ID
- 默认立绘
- 多个表情立绘
- Qwen 参考音频 / 文本
- Index 参考音频
- 默认语言策略
- 默认引擎策略

示例：

```yaml
id: special_week
display_name: Special Week
default_sprite: default.png

engine_policy:
  ja: qwen
  zh: index

sprites:
  neutral: default.png
  smile: smile.png

qwen:
  ref_audio: voice/ref_ja.wav
  ref_text: voice/ref_ja.txt
  prompt_cache_key: special_week_ja_v1

index:
  spk_audio_prompt: voice/ref_ja.wav
  default_emo_alpha: 0.6
```

### 7.2 `script.json`

负责表达“语义层”的内容，也就是剧本本身：

```json
{
  "projectId": "ep001",
  "title": "草地上的对话",
  "fps": 30,
  "scenes": [
    {
      "id": "scene_001",
      "background": "projects/ep001/scenes/grass_day.png",
      "scenePrompt": "anime meadow, sunny day, breeze, no people, no text",
      "characters": [
        {"speakerId": "special_week", "slot": "left"},
        {"speakerId": "silence_suzuka", "slot": "right"}
      ],
      "lines": [
        {
          "id": "l001",
          "type": "dialogue",
          "speakerId": "special_week",
          "spokenLang": "ja",
          "spokenText": "ねえ、今日は風が気持ちいいね。",
          "subtitleJa": "ねえ、今日は風が気持ちいいね。",
          "subtitleZh": "呐，今天的风很舒服呢。",
          "emotion": "gentle"
        },
        {
          "id": "l002",
          "type": "narration",
          "speakerId": "narrator",
          "spokenLang": "zh",
          "spokenText": "草地上的风轻轻吹过。",
          "subtitleZh": "草地上的风轻轻吹过。"
        }
      ]
    }
  ]
}
```

### 7.3 `timeline.json`

负责表达“渲染层”的内容，也就是最终怎么播：

```json
{
  "projectId": "ep001",
  "fps": 30,
  "width": 1920,
  "height": 1080,
  "durationInFrames": 450,
  "tracks": [
    {
      "sceneId": "scene_001",
      "background": "projects/ep001/scenes/grass_day.png",
      "characters": [
        {"speakerId": "special_week", "sprite": "assets/characters/special_week/default.png", "slot": "left"},
        {"speakerId": "silence_suzuka", "sprite": "assets/characters/silence_suzuka/default.png", "slot": "right"}
      ],
      "segments": [
        {
          "lineId": "l001",
          "type": "dialogue",
          "speakerId": "special_week",
          "audio": "projects/ep001/audio/l001.wav",
          "startFrame": 0,
          "endFrame": 78,
          "subtitleJa": "ねえ、今日は風が気持ちいいね。",
          "subtitleZh": "呐，今天的风很舒服呢。",
          "activeSpeaker": "special_week"
        }
      ]
    }
  ]
}
```

---

## 8. 核心实现方式

### 8.1 剧本解析

输入可以是：

- 纯文本小说
- Markdown 剧本
- 手工整理好的对白稿

解析器需要做的事情：

- 识别场景边界
- 识别对白与旁白
- 给对白绑定角色
- 给句子加上语言、情绪、字幕字段
- 尽量保留“原始文本”和“渲染文本”两层

推荐原则：

- 原始输入可以是中文
- 演绎台词可以默认生成日语版本
- 中文字幕始终保留
- 旁白可按项目需要决定用中文还是日语

### 8.2 场景规划

如果你已经准备好背景图，则直接绑定。

如果没有背景图，就只做一件事：

> **生成一个高质量的场景 prompt，并把它落到 JSON 里。**

这样你就可以在自己熟悉的生图工具里补图，而不用把项目耦合进图像模型本身。

### 8.3 TTS 统一接口

不管底层是 Qwen 还是 Index，对上层都应该长成一个统一接口：

```python
synthesize_line(
    speaker_id="special_week",
    text="ねえ、今日は風が気持ちいいね。",
    lang="ja",
    emotion="gentle",
    output_path="projects/ep001/audio/l001.wav"
)
```

底层再由适配层决定：

- 用 Qwen3-TTS 还是 IndexTTS2
- 要不要加载角色缓存 prompt
- 要不要带情绪参考音频
- 要不要带情绪向量

### 8.4 默认路由策略

建议先把策略写死，后面再开放覆盖：

- `spokenLang == ja` -> `qwen`
- `spokenLang == zh` -> `index`

单句如果要覆盖，再在脚本里写：

```json
{
  "id": "l007",
  "speakerId": "special_week",
  "spokenLang": "zh",
  "engine": "qwen"
}
```

### 8.5 时间轴生成

这一步必须放在 TTS 之后。

原因很简单：

> **音频真实时长才是当前项目里最可靠的时间轴来源。**

你不做嘴型，所以没有必要提前对齐 phoneme。只需要：

- 读取音频时长
- 转成帧数
- 顺序累加开始时间 / 结束时间
- 为字幕和角色激活状态生成区间

### 8.6 Remotion 演出层

视频层只做这些事情：

- 场景背景铺底
- 左右放角色 PNG
- 当前说话角色轻微上浮 + 缩放 + 提亮
- 非当前角色轻微变暗
- 底部显示双语字幕
- 旁白用独立样式显示
- 输出视频

第一版建议的角色说话动画很简单：

- 上下浮动：6px ~ 12px
- 缩放：1.00 -> 1.03 -> 1.00
- 透明度或亮度轻微变化

这足够表达“谁在说话”，而不会引入嘴型同步的复杂度。

---

## 9. 推荐 CLI 设计

建议把整个项目统一成一个总命令，再拆子命令：

```bash
python main.py init ep001
python main.py parse projects/ep001/input/novel.md
python main.py plan-scenes projects/ep001/work/script.json
python main.py tts projects/ep001/work/script.json
python main.py build-timeline projects/ep001/work/script.json
python main.py render projects/ep001/work/timeline.json
python main.py all projects/ep001/input/novel.md
```

### 各命令职责

- `init`：创建项目目录
- `parse`：原始文本 -> `script.json`
- `plan-scenes`：补场景 prompt / 绑定背景图
- `tts`：逐句生成音频
- `build-timeline`：音频 -> `timeline.json`
- `render`：调用 Remotion 出视频
- `all`：串行执行全部步骤

这套命令的好处是：

- 可以从中间步骤恢复
- 失败后易于重试
- 便于以后封装成 MCP / skills

---

## 10. 推荐补充目录

当前仓库已经有 `my_tts/` 和 `my-video/`，建议下一步补上两个目录：

```text
assets/
  characters/
  scenes/
  bgm/
  sfx/
  cache/

projects/
  ep001/
    input/
    work/
    audio/
    scenes/
    output/
```

### `assets/`

放全局可复用素材：

- 角色立绘
- 角色参考音频
- 通用 BGM / SFX
- TTS prompt 缓存

### `projects/`

放单次视频项目：

- 输入文本
- 中间 JSON
- 本次生成音频
- 本次场景图
- 输出视频

这样全局素材和单次项目不会混在一起。

---

## 11. 分阶段开发规划

### Phase 0：跑通最小闭环

目标：

- 手工写 `script.json`
- 准备 2 个角色 PNG + 1 张背景图
- 手工调用 TTS 生成几句音频
- 手工写 `timeline.json`
- 在 Remotion 里渲染出一个 10~30 秒的视频

验收标准：

- 有背景图
- 有双角色站位
- 谁说话谁轻微跳动
- 有双语字幕
- 能正常导出 MP4

### Phase 1：自动化文本到视频

目标：

- 从 `novel.md` 自动生成 `script.json`
- 自动调用 TTS
- 自动读音频时长生成 `timeline.json`
- 一条命令产出视频

验收标准：

- 不再手工改时间轴
- 支持多句对白与旁白
- 支持至少 2 个场景切换

### Phase 2：场景规划与素材管理

目标：

- 当缺背景图时自动生成场景 prompt
- 固化 `speaker.yaml`
- 增加素材缓存与复用
- 引入 `projects/` 与 `assets/` 目录体系

验收标准：

- 新项目可以复用老角色配置
- 相同角色多句对白不重复做无意义准备
- 无背景图时仍能继续项目流程

### Phase 3：可扩展流水线

目标：

- 封装 MCP / skills
- 支持批量渲染多个剧本
- 支持 BGM / SFX
- 支持更细的情绪控制

验收标准：

- 外部 agent 可以调用该流水线
- 支持多项目并行管理
- 支持更稳定的角色音色一致性

---

## 12. MVP 成功标准

当以下输入可以稳定产出视频时，就说明第一阶段成功了：

### 输入

- 1 份 Markdown 剧本
- 2 个角色 PNG
- 2 份角色参考音频
- 1~3 张背景图

### 输出

- 1 个 1080p MP4
- 至少 30 秒内容
- 双语字幕可读
- 旁白可正常插入
- 当前说话角色有明显但不过度的演出提示

---

## 13. 非功能性原则

### 本地优先

优先支持本地运行：

- 模型本地部署
- 素材本地缓存
- 视频本地渲染

### 可恢复

任何一步失败，都应该支持从中间继续，而不是整条链重跑。

### 可替换

- TTS 引擎可替换
- 场景生图工具可替换
- 渲染主题可替换

### 可追踪

每个项目都应该保留：

- 输入文本
- 中间 JSON
- 输出音频
- 输出视频
- 关键日志

---

## 14. 已知风险

- 赛马娘素材来源需要注意版权与二创边界
- 角色语音素材不一定每个角色都完整
- 跨语种克隆的稳定性依赖参考音频质量
- 如果后续要做口型同步，工程复杂度会明显上升

因此当前项目建议定位为：

> **本地研究 / 个人创作辅助 / 轻演出生成工具**

而不是一开始就追求商业级动画生产线。

---

## 15. 一句话总结

`umamusume-anime` 的核心不是“把每个模块都做到最强”，而是把它们拼成一条稳定的本地流水线：

- 用 `my_tts` 统一两套 TTS
- 用 `my-video` 负责视频渲染
- 用 `main.py` 做总编排
- 用 `script.json` / `timeline.json` 做数据契约
- 先做“无嘴型、轻演出”的 MVP
- 再逐步扩展到更完整的剧情视频生成系统

