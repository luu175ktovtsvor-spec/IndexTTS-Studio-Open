# IndexTTS Studio

IndexTTS Studio 是基于 IndexTTS 2.5 的本地可视化工作台。参考音频、台词和生成结果由运行服务的设备处理。

项目同时提供 IndexTTS 命令行、Gradio WebUI 和 Studio 界面。

## 能做什么

- 导入音频或视频；浏览器录音可选择系统提供的麦克风、声卡或虚拟音频输入设备。
- 界面可切换中文、English、日本語、Español 和 العربية；界面语言与生成语言相互独立。
- 支持中文、英文、日文、西班牙文和阿拉伯文，以及情绪、语速和精细参数控制。
- 支持拼音、英文 CMU 音素和日语假名标注。
- 提供预设、试音案例、生成历史、实时 Token / 分段进度、状态恢复和多格式导出。

## macOS 支持

- CUDA、DeepSpeed 和 CUDA 内核用于 NVIDIA GPU 加速。Studio 另提供 Apple silicon 的 ARM64 / MPS 运行路径。
- Mac 路径面向 M1 及后续 Apple M 系列芯片，包括 Pro、Max 和 Ultra。主体推理使用 MPS，BigVGAN 声码器使用 CPU 兼容路径。
- Intel Mac 为 x86_64，不属于该 Mac 加速路径的适用范围。

## 安装与启动

前提：Python 3.10 或 3.11、[uv](https://docs.astral.sh/uv/)、以及 `ffmpeg`。macOS 可用 `brew install ffmpeg`；Windows/Linux 请使用对应系统的安装方式。

```bash
git clone https://github.com/luu175ktovtsvor-spec/IndexTTS-Studio-Open.git
cd IndexTTS-Studio-Open

uv sync --extra studio --locked

# 下载 IndexTTS 2.5 权重到 checkpoints/
uv tool install huggingface-hub
hf download IndexTeam/IndexTTS-2.5 --local-dir=checkpoints

# 完整校验官方模型文件；首次下载后建议运行一次
uv run python -m indextts.utils.model_integrity checkpoints

# 启动工作台
uv run --extra studio --locked python studio_server.py
```

随后打开 `http://127.0.0.1:7860`。

macOS/Linux 也可运行 `./start-studio.sh`。如需更换端口：

```bash
INDEXTTS_STUDIO_PORT=7861 ./start-studio.sh
```

默认和公开交付界面是自写 Studio。`INDEXTTS_STUDIO_PORT` 只更换 Studio 端口，不代表第二套界面。上游原生 Gradio WebUI 为可选入口，可在另一个终端运行：

```bash
./start-native-webui.sh
# 默认 http://127.0.0.1:7861
```

Studio 与原生 WebUI 可同时打开，但会使用两个独立模型进程并增加内存占用，因此默认启动器只启动 Studio。关闭对应启动终端即可停止该服务。

回归测试命令：

```bash
./tools/test-studio.sh
```

权重位于其他目录时，可设置 `INDEXTTS_CHECKPOINTS_DIR=/path/to/checkpoints`。

第一次打开时会自动下载试音案例，可能需要等待片刻。

## 说明

- 服务默认只监听本机。远程部署并使用录音功能时需要 HTTPS。
- 首次录音需允许浏览器访问麦克风；系统声音只有在操作系统将其提供为音频输入设备时才会出现在列表中。
- 音频文件上限为 100 MB，视频文件上限为 1 GB。导入长音频或视频后，可选择参考片段起点；模型从该位置使用 15 秒声音。自然口播节奏支持分别设置句间和段落停顿。
- 工作台会检查参考声音的时长、音量、静音和削波；多人说话与具体噪声类型仍需人工试听确认。
- 单次台词上限为 20,000 个字符；同一服务同时只运行一个生成任务，运行中可从顶部状态栏取消。
- 生成历史可单条删除或清空，并自动保留最近 100 条或最多 5 GB，达到任一上限时删除最早记录。
- 服务拒绝来自非本机网页的修改请求。默认仅监听 `127.0.0.1`，不要在没有额外鉴权的情况下改为公网监听。
- 预设和生成结果默认保存在 `outputs/`。
- 模型权重需要单独下载；许可信息见 [LICENSE](LICENSE) 和 [OPEN_SOURCE_NOTICE.md](OPEN_SOURCE_NOTICE.md)。
