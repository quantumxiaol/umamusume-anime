# RoleTone

基于 WavLM 的 TTS clone 音色相似度评判服务。输入一段用于 clone 的参考音频，以及一批生成音频，输出可排序、可阈值化的相似度分数。

默认模型是 `microsoft/wavlm-base-plus-sv`，它带 speaker-verification/x-vector 头，更适合判断“是否像同一个音色”。如果你明确要跑通用 WavLM，也可以用 `--model base` 或 `--model large`，此时会使用 hidden state mean pooling。

## 安装

本项目开发环境：

```bash
uv sync
cp .env.example .env
```

作为 GitHub 依赖安装：

```bash
pip install "git+https://github.com/<USER>/<REPO>.git@main"
```

在 `requirements.txt` 中使用：

```text
roletone @ git+https://github.com/<USER>/<REPO>.git@main
```

如果是私有仓库，可以用 SSH：

```bash
pip install "git+ssh://git@github.com/<USER>/<REPO>.git@main"
```

`.env` 默认从当前运行目录读取。相对路径也以当前运行目录为基准，所以可以在你的业务项目目录中放一个 `.env`：

```bash
HF_HOME=./modelsweights/huggingface
HF_HUB_DISABLE_XET=1
ROLETONE_MODEL=wavlm-base-plus-sv
ROLETONE_DEVICE=auto
```

如果你不在业务项目目录运行，可以显式指定运行根目录：

```bash
export ROLETONE_HOME=/path/to/your/audio-project
```

或者显式指定配置文件：

```bash
export ROLETONE_ENV_FILE=/path/to/your/audio-project/.env
```

如果你想直接指定 `transformers.from_pretrained(cache_dir=...)` 使用的目录，可以加：

```bash
ROLETONE_CACHE_DIR=./modelsweights/transformers-cache
```

如果模型已经下载好，运行时不希望访问 HuggingFace，可以加：

```bash
ROLETONE_LOCAL_FILES_ONLY=1
```

也可以不用 `.env`，直接在命令前指定：

```bash
HF_HOME=./modelsweights/huggingface roletone download --model sv
```

同样的配置也可以全部写在命令行参数里：

```bash
roletone download \
  --model sv \
  --hf-home ./modelsweights/huggingface \
  --device cpu
```

## 下载模型

```bash
uv run roletone download --model sv
```

如果是 `pip install git+...` 安装，直接运行：

```bash
roletone download --model sv
```

下载通用 WavLM：

```bash
uv run roletone download --model base
uv run roletone download --model large
```

可用别名：

- `sv` / `base-sv`: `microsoft/wavlm-base-plus-sv`
- `base` / `base-plus`: `microsoft/wavlm-base-plus`
- `large`: `microsoft/wavlm-large`

`--model` 也可以传完整 HuggingFace repo id 或本地模型目录：

```bash
roletone download --model microsoft/wavlm-base-plus-sv
roletone score --model ./modelsweights/my-wavlm --reference ref.wav --candidate gen.wav
```

查看当前可用计算设备：

```bash
roletone devices
```

`--device` 支持：

- `auto`: 自动选择 `cuda`、`mps`、`cpu`
- `cpu`
- `mps`: Apple Silicon GPU
- `cuda` / `cuda:0`: NVIDIA GPU

## 命令行打分

单个或多个文件：

```bash
uv run roletone score \
  --reference inputs/reference.wav \
  --candidate outputs/line_001.wav \
  --candidate outputs/line_002.wav \
  --format json
```

批量目录：

```bash
uv run roletone score-dir \
  --reference inputs/reference.wav \
  --candidates-dir outputs \
  --pattern "*.wav" \
  --output outputs/scores.csv
```

`pip install git+...` 后对应命令：

```bash
roletone score-dir \
  --reference inputs/reference.wav \
  --candidates-dir outputs \
  --pattern "*.wav" \
  --output outputs/scores.csv \
  --device mps \
  --model sv \
  --hf-home ./modelsweights/huggingface
```

只使用已经下载好的模型：

```bash
roletone score-dir \
  --reference inputs/reference.wav \
  --candidates-dir outputs \
  --pattern "*.wav" \
  --output outputs/scores.csv \
  --offline
```

输出字段：

- `cosine`: 原始 cosine 相似度，建议主要看这个值。
- `similarity`: 把 cosine 映射到 `0..1` 的值。
- `score`: `similarity * 100`，便于展示。
- `verdict`: 按阈值给出的粗分类。

默认阈值：

- `match`: cosine >= `0.86`
- `likely_match`: cosine >= `0.75`
- `borderline`: cosine >= `0.65`
- `mismatch`: cosine < `0.65`

这些阈值不是通用真理，建议拿你自己的“明显像角色”和“明显不像角色”的样本做一次校准。

## 启动服务

```bash
uv run roletone serve --host 127.0.0.1 --port 8000
```

`pip install git+...` 后对应命令：

```bash
roletone serve --host 127.0.0.1 --port 8000
```

服务模式也可以指定模型、权重目录和设备：

```bash
roletone serve \
  --host 127.0.0.1 \
  --port 8000 \
  --model sv \
  --device mps \
  --hf-home ./modelsweights/huggingface \
  --offline
```

接口：

```bash
curl http://127.0.0.1:8000/health
```

Swagger 文档在：

```text
http://127.0.0.1:8000/docs
```

上传文件评分：

```bash
curl -X POST http://127.0.0.1:8000/score \
  -F reference=@inputs/reference.wav \
  -F candidate=@outputs/line_001.wav
```

本地路径批量评分：

```bash
curl -X POST http://127.0.0.1:8000/score/paths \
  -H "Content-Type: application/json" \
  -d '{
    "reference": "inputs/reference.wav",
    "candidates": ["outputs/line_001.wav", "outputs/line_002.wav"]
  }'
```

## Python 调用

```python
from roletone.scorer import WavLMScorer

scorer = WavLMScorer()
result = scorer.compare_files(
    "inputs/reference.mp3",
    "inputs/sd/sd002.wav",
)

print(result.to_dict())
```

## 说明

这个服务衡量的是参考音频和生成音频在 WavLM embedding 空间里的音色/说话人相似度。它不能直接判断台词情绪、表演风格、咬字自然度或文本内容是否正确；这些维度需要额外指标。
