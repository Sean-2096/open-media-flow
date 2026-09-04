# OpenMediaFlow

> Plan, generate, review, and publish everywhere — locally.

OpenMediaFlow 是一个本地优先的全自动内容运营项目，目标平台为抖音、小红书、
哔哩哔哩和 YouTube。v0.7 已把“内容生成”提升为主链路：系统先生成完整内容包和分镜，
再生成封面、视频镜头与旁白，完成合成、审核，最后进入多平台发布门禁。

真实发布仍固定为 `dry-run`，不会改动任何外部账号。平台 OAuth、Cookie 和验证码接管
应在生成与审核链路稳定后分别接入。

## 架构

```text
主题与运营目标
  → Qwen：标题 / 脚本 / 标签 / 受众 / Hook / 分镜 / 视觉提示词
  → ComfyUI：封面 + 每个分镜的视频镜头
  → macOS 原生媒体运行时：中文旁白
  → 内置 MoneyPrinterTurbo：镜头 / 配音 / 字幕 / 转场合成
  → FFprobe + 规则 + LLM 多模态审核门禁
  → 抖音 / 小红书 / B站 / YouTube 发布适配器
  → 数据回流与下一轮选题（待接入平台授权）
```

核心组件：

- `api`：任务、自动化、审核和发布控制面。
- `APScheduler + PostgreSQL + Redis`：内置编排、持久化和防重复锁，不依赖 n8n。
- `llama.cpp`：本地 Qwen 文本策划；可替换为任意 OpenAI-compatible 端点。
- `ComfyUIProvider`：本地图片和视频生成接口；工作流随项目版本管理。
- `local_media_runtime`：运行在 macOS 宿主机，调用系统语音和硬件能力。
- `video-engine`：项目内嵌的 MoneyPrinterTurbo 合成引擎。
- `MinIO`：预留的产物归档服务。

Docker 负责隔离服务，不会自动带来模型，也无法在 macOS 上直接获得 Metal/MPS GPU。
因此数据库、API 和合成引擎运行在 Docker；LLM、ComfyUI、神经配音与 AI 插帧运行在宿主机，
但它们的启动脚本、配置、工作流和产物仍全部由本项目管理。

对使用者而言，ComfyUI 只是 OpenMediaFlow 当前采用的内部画面执行引擎，不是需要单独
操作的产品。控制台、任务状态、重试、日志与生命周期均由 OpenMediaFlow 统一管理。
后续可在媒体 Provider 边界替换为 Diffusers 直连实现，业务编排与页面无需随之改写。

## 单项目边界

```text
open-media-flow/
├── src/open_media_flow/       # API、状态机、模型路由、媒体 Provider、审核
├── services/video-engine/     # 内置 MoneyPrinterTurbo 源码
├── scripts/                   # 本机 LLM 与媒体运行时启动脚本
├── config/workflows/          # ComfyUI API 工作流
├── config/policy.json         # 审核规则
└── data/                      # 模型、生成素材、音频、视频与运行期数据
```

项目不再读取 `../MoneyPrinterTurbo`。内置版本来源和本地修改见
`services/video-engine/UPSTREAM.md`。

## 当前可用状态

- 已可用：Qwen 完整内容包与分镜、Postgres 自动状态机、Qwen3-TTS 神经配音、
  角色母版驱动的 LTX 图生视频、RIFE-MLX 24→48 FPS 插帧、视频合成、
  ComfyUI 图片/视频生成、FFprobe/规则/LLM 审核、dry-run 发布。
- 已接入口型编排框架：计划可选旁白、混合讲述或正面讲话；讲话分镜拥有独立音频、
  运行任务、同步分数、正脸覆盖率、重试和旁白降级记录。Apple Silicon 可运行
  `./scripts/install-lip-sync-runtime.sh` 安装隔离的 MuseTalk v1.5 MPS 运行时；示例配置
  默认关闭，安装后再将 `OMF_LIP_SYNC_ENABLED` 设为 `true`。内部推理引擎使用 8091，
  现有 8090 媒体服务统一提供异步任务、阶段耗时、质量检测与安全路径校验。
- 已选本机模型：RealVisXL V5.0 Lightning FP16 负责写实角色母版与封面，
  LTX-Video 2B distilled 负责竖屏视频镜头；模型
  权重和 ComfyUI 运行时均保存在 `data/comfyui`。
- 首次真实验证完成前，`.env` 保持 `OMF_MEDIA_GENERATION_ENABLED=false`。自动任务会在
  `planned` 状态安全等待，不会悄悄回退成“拿库存素材假装 AI 生成”。验证完成后再启用。
- `video_materials` 仅是可选的人工回退输入，不再是自动化计划的必填项。

## 启动

首次配置：

```bash
cp .env.example .env
```

至少修改 `.env` 中标记为 `change-me` 的本地密码。首次使用先安装本机媒体运行时
（模型会保存在项目 `data/models`，以后无需重复下载）：

```bash
./scripts/install-media-runtime.sh
```

安装完成后，使用一个命令启动完整产品：

```bash
./scripts/omf start
```

该命令会统一启动 Docker 服务、内容模型、画面生成引擎、配音和动态增强运行时。
无需分别维护多个终端。常用运维命令：

```bash
./scripts/omf status
./scripts/omf logs generation
./scripts/omf stop
```

入口：

- 控制台：http://127.0.0.1:8000/
- OpenMediaFlow API：http://127.0.0.1:8000/docs
- 内置视频引擎：http://127.0.0.1:8082/docs
- 本机媒体运行时：http://127.0.0.1:8090/health
- MinIO：http://127.0.0.1:9001

不需要注册或登录工作流平台。通过控制台首页进入时会自动建立同源本地会话；一般无需
手工输入 API KEY。

## 自动流程

### AI 动态漫剧 V1

创建计划时可选择 `ai_comic` 内容类型。它不会复用写实口播提示词，而是生成角色设定、
故事梗概、对白、情绪、景别、运镜和连续性字段；每个分镜固定生成为动漫关键帧，再由
宿主机媒体运行时渲染为 1080×1920、48 FPS 的稳定二维镜头。这样可以避免视频模型逐帧
重画角色导致的脸型、服装和肢体漂移，也不会把真人 MuseTalk 嘴部融合到动漫角色上。

漫剧模式使用独立的 `comic_image.json` 工作流和 Animagine XL 4.0 Opt checkpoint，不会替换
通用短视频使用的写实 RealVisXL。首次安装与目测验证通过前保持
`OMF_COMIC_GENERATION_ENABLED=false`；角色一致性增强下一阶段再接入 IP-Adapter 和 ControlNet。

漫剧生产链路：

```text
主题 → 原创角色与本集剧情 → 6–10 个对白分镜 → 动漫关键帧
     → hold / push / pull / pan 二维运镜 → 48 FPS 镜头 → 配音字幕 → 合成审核
```

载入本地 API 密钥：

```bash
set -a
source .env
set +a
```

创建一个默认禁用的计划，先验证单次运行：

```bash
curl -X POST http://127.0.0.1:8000/automations \
  -H 'Content-Type: application/json' \
  -H "X-API-Key: $OMF_API_KEY" \
  -d '{
    "name": "每日 AI 工具观察",
    "topic": "值得普通用户关注的本地 AI 工具",
    "platforms": ["douyin", "xiaohongshu", "bilibili", "youtube"],
    "interval_minutes": 1440,
    "enabled": false
  }'
```

立即触发：

```bash
curl -X POST http://127.0.0.1:8000/automations/<automation-id>/run \
  -H "X-API-Key: $OMF_API_KEY"
```

状态机会依次经过：

```text
draft → planned → assets_generating → lip_syncing（按需）→ composing → generated
      → approved / review_rejected → published / partial_failure
```

`assets_generating` 会先生成一张原创角色母版，再让全部 LTX-Video 分镜引用同一形象；
原始 24 FPS 分镜随后通过 RIFE-MLX 插值为 48 FPS，再交给合成器。最终文件也以
48 FPS 编码，避免只有高帧率标签、没有真实中间运动帧。配音默认使用项目内的
Qwen3-TTS 1.7B 8-bit 模型和 `Vivian`
中文音色，可通过 `.env` 的 `OMF_TTS_VOICE` 与 `OMF_TTS_INSTRUCT` 调整。

正面讲话分镜会使用 768×1024、五步 DPM++ SDE Karras 采样的写实腰部以上角色母版作为稳定口型底片，
而不是把已有自主嘴部运动的普通视频二次改嘴；原始动态分镜仍保留为质量失败时的
旁白回退素材。随后系统生成独立驱动音频，再进入可插拔唇形运行时。运行时必须返回
`sync_score` 和 `face_coverage`；低于配置门槛时，系统自动保留原始视频并降级为旁白。
未安装模型时选择讲话模式也不会卡住任务，详情页会明确展示降级原因。

MuseTalk 的默认融合参数针对本机写实人物做了保守收口：缩小两颊和下颌替换范围，
并对单张角色母版启用轻量时序稳定，降低唇缘闪烁与整块下巴被替换的观感；动态人物
视频不会启用前帧融合，以免转头时拖影。可通过 `.env` 中的 `MUSETALK_EXTRA_MARGIN`、
`MUSETALK_LEFT_CHEEK_WIDTH`、`MUSETALK_RIGHT_CHEEK_WIDTH`、
`MUSETALK_UPPER_BOUNDARY_RATIO` 和 `MUSETALK_TEMPORAL_SMOOTHING` 微调。

口型阶段支持三档策略：`auto` 优先调用 LatentSync 1.6 质量 Worker，离线时自动使用
MuseTalk MPS；`fast` 固定使用本机 MuseTalk；`quality` 固定等待 LatentSync，不会把
低质量结果伪装成高质量结果。CUDA Worker 的统一适配器位于
`services/latentsync-worker`，连接地址通过 `OMF_LATENTSYNC_BASE_URL` 配置。

在媒体模型尚未启用时，流程停在 `planned`，运行记录显示
`waiting_for_media_runtime`。安装模型和工作流后将 `OMF_MEDIA_GENERATION_ENABLED=true`，
重启 API，已有任务会继续推进。

## 手动验证内容策划

创建任务后调用完整内容包生成接口：

```text
POST /tasks
POST /tasks/{id}/generate-content-plan
GET  /tasks/{id}
```

响应中的 `content_plan` 包含受众、Hook、创意方向、封面提示词和 3–12 个分镜。
`metadata.llm_generation` 记录实际模型和端点，但不会记录 API Key。

旧的手动合成接口仍保留：

```text
POST /tasks/{id}/generate-metadata
POST /tasks/{id}/generate-video
POST /tasks/{id}/media
POST /tasks/{id}/audit
POST /tasks/{id}/publish
```

人工素材路径相对于 `data/inbox`，服务端会阻止目录穿越。生成产物写入
`data/output/video-engine`。

## 模型端点

主链路统一调用 `/v1/chat/completions`，可使用 llama.cpp、LM Studio、Ollama、
MLX-LM 或兼容服务。

OpenRouter 只在明确启用后作为回退：

```env
LLM_FALLBACK_ENABLED=true
LLM_FALLBACK_BASE_URL=https://openrouter.ai/api/v1
LLM_FALLBACK_MODEL=<model-slug>
LLM_FALLBACK_API_KEY=<OPENROUTER_API_KEY>
OPENROUTER_ZDR=true
OPENROUTER_DATA_COLLECTION=deny
```

Hugging Face Inference Providers 也可作为 OpenAI-compatible 回退端点：

```env
LLM_FALLBACK_ENABLED=true
LLM_FALLBACK_BASE_URL=https://router.huggingface.co/v1
LLM_FALLBACK_MODEL=<organization>/<model>:fastest
LLM_FALLBACK_API_KEY=<HF_TOKEN>
```

默认仅将 Hugging Face 用作本地模型仓库，不进入在线推理主链路。

## ComfyUI 工作流接口

Provider 读取：

- `config/workflows/image.json`
- `config/workflows/comic_image.json`
- `config/workflows/video.json`

工作流必须以 ComfyUI 的 API Format 导出，并可使用
`{{PROMPT}}`、`{{NEGATIVE_PROMPT}}`、`{{WIDTH}}`、`{{HEIGHT}}`、
`{{DURATION_SECONDS}}`、`{{FRAME_COUNT}}`、`{{SEED}}`、
`{{FILENAME_PREFIX}}` 占位符。详细说明见 `config/workflows/README.md`。
本地大模型的来源、校验值和许可证记录见 `config/MODELS.md`。

## 安全边界

- 默认发布器始终是 `dry-run`。
- 媒体和自定义旁白只能来自项目 `data/inbox` 白名单目录。
- 不把 Cookie、OAuth token、平台密码写入仓库或自动化内容。
- OpenRouter 默认关闭；启用时默认请求 ZDR 并禁止数据采集路由。
- 小红书、抖音的非官方浏览器自动化需要独立 Profile，并保留扫码/验证码人工接管。
- 正式发布前需分别实现官方 API 适配、幂等键、作品 ID 回查、失败告警与撤回策略。

## 开发验证

```bash
.venv/bin/pytest -q
.venv/bin/ruff check src tests
docker compose config
```
