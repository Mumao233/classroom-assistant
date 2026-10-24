# 课间 · Classroom Assistant v1.1.1

面向 Windows 线下课堂的本地学习助手：用麦克风分段转写以英语为主、夹杂中文的讲课内容，再按意群解释，并支持随时打字提问。语音识别在本机运行，文字分析可选择已登录的本地 Codex CLI，或自己配置的 OpenAI 兼容 API。

这是可试用的个人项目。字幕按片段更新，不是逐字零延迟字幕；真实教室的识别效果和持续两小时的续航尚未验证。

![课间界面：合成课堂示例](docs/preview.jpg)

预览使用合成示例，不是真实课堂记录。验证范围见 [验证记录](docs/VALIDATION.md)。

## 能做什么

- 中英混合转写、时间标记、原音频回听、低置信提示；可修改文字并保留机器原稿。
- 左侧原文、中间意群解释、右侧问答；支持专注模式、回看和回到实时。
- 自动 / 电池省电 / 插电高质量三种模式；显示实际模型、设备、处理队列与转写积压。
- 微积分、微观经济学课程预设，可新增、改名、补充大纲并通过 Codex 生成术语。
- 十秒试音、录音失败时手动补充原文、网络或 AI 失败后恢复；录音与 AI 队列分开处理。
- 课后手动高质量重转写，修订稿另存；JSON / Markdown 导出，也可只删除录音、保留文字。
- 在网页「AI 接入设置」中切换本地 Codex / API；API 解释和问答边生成边显示，完整校验后才存为正式结果。
- API 密钥可仅本次使用，或选择 Windows 加密保存；显示当前课堂 API 调用次数与已返回的 token 用量。

版本变化见 [CHANGELOG](docs/CHANGELOG.md)。`v1.0.0` 对应首次发布提交 `8c07b1e`。

## 安装

需要 Windows、Python **3.10 或更新版本**、新版 Edge 或 Chrome。前端无打包步骤；Node.js 仅用于下方的 JavaScript 测试。如果选择用 npm 安装 Codex，则该安装方式也需要 Node.js。

### 1. 创建运行环境

下载或克隆仓库，在项目目录打开 PowerShell，执行：

```powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

脚本创建 `.venv`、安装 `requirements.txt`，并在没有 `config.json` 时从 `config.example.json` 复制一份。已有配置不会覆盖。脚本不会下载语音模型或配置 NVIDIA 运行库。

### 2. 准备本地语音模型

完整模式需要两个 **faster-whisper / CTranslate2 格式**的多语言模型，放到：

```text
models/
  faster-whisper-small/
  faster-whisper-large-v3-turbo/
```

兼容模型下载参考：

| 用途 | 模型仓库 |
| --- | --- |
| CPU 省电模式 | [Systran/faster-whisper-small](https://huggingface.co/Systran/faster-whisper-small) |
| GPU 高质量模式 | [mobiuslabsgmbh/faster-whisper-large-v3-turbo](https://huggingface.co/mobiuslabsgmbh/faster-whisper-large-v3-turbo) |

large 模型链接目前会跳转至 `dropbox-dash/faster-whisper-large-v3-turbo`。这些链接是兼容格式的下载参考，不构成本项目历史测试权重的来源校验。

下载模型仓库的完整实际文件，保留 `model.bin`、`config.json`、`tokenizer.json`、`vocabulary.json` 或 `vocabulary.txt`，以及仓库中的其他模型配套文件。不要只保存模型网页、下载指针或英文专用的 `.en` 模型。模型权重不包含在本仓库中。

默认配置使用上面的相对路径，也可修改 `config.json`：

```json
{
  "mode": "auto",
  "cpu_model_path": "models/faster-whisper-small",
  "gpu_model_path": "models/faster-whisper-large-v3-turbo"
}
```

只先准备 small 模型也可以试用 CPU 转写；GPU 功能需要完整大模型和可用的 NVIDIA 环境。

### 3. 可选：配置 NVIDIA GPU

高质量模式需要接通电源、可用 NVIDIA 独显，以及与 CTranslate2 匹配的 CUDA、cuBLAS、cuDNN 运行库。请按 [faster-whisper 官方 GPU 安装说明](https://github.com/SYSTRAN/faster-whisper#gpu) 配置，不要仅凭已安装显卡驱动判断依赖齐全。

如 DLL 不在当前环境可发现的位置，可在 `config.json` 的 `cuda_library_dirs` 中填写对应的本机目录。程序不会替你开启独显或修改系统电源设置。条件不足时，界面会说明 CPU 回退；模式切换在音频片段边界进行。

### 4. 选择 AI 接入方式

启动网页后打开「AI 接入设置」，选择「本地 Codex」或「API」。两种方式同时支持意群解释、课堂问答和术语生成；三个功能共用所选模型。API 模式不要求安装或登录 Codex。

#### 本地 Codex

按 [OpenAI Codex 官方安装说明](https://github.com/openai/codex#quickstart) 安装 CLI，确保 PowerShell 能执行 `codex`，然后运行：

```powershell
codex --version
codex login
codex login status
```

登录时使用自己的 ChatGPT 账号。账号是否可用、模型权限与额度由 Codex / ChatGPT 决定；本项目不提供额外额度，也不要求把 API Key 填入项目。

**CLI 兼容性：已测试版本为 `0.163.0-alpha.11.1`。** 桥接使用 `codex exec` 的较新选项，包括 `--ignore-user-config`、`--ignore-rules` 和 `--ephemeral`。其他版本需核对 `codex exec --help`，不能假定任意稳定版或旧版本都兼容。若出现“不认识参数”，先检查版本和 [桥接实现](codex_bridge.py)，不要直接删掉隔离选项。

#### OpenAI 兼容 API

在网页填写 **API 根地址、API Key、模型名**。例如根地址可为 `https://api.openai.com/v1`，但应以你选择的服务商文档为准，模型名也必须使用该服务商支持的名称。程序使用 Chat Completions 的 `/chat/completions` 接口；不支持原生 Anthropic、Gemini 或仅 Responses API 的服务。服务商需兼容 `system` / `user` 消息，以及流式请求的 `stream_options.include_usage`；若拒绝这些字段，会显示错误并等待你处理，不自动换接口重复计费。服务不返回用量时，相关 token 数量记为未知。

「测试连接」只发送简短合成文字，可能产生少量 API 费用；测试草稿不会自动保存，也不会发送课堂内容。「保存」不会自动发测试请求。

API Key 留空表示保留同地址已配置的密钥；更换 API 地址后必须重新输入。默认密钥只留在后端内存，服务重启后需重填。勾选「记住密钥」后使用当前 Windows 用户的 DPAPI 加密保存，换电脑或 Windows 用户需重新配置；可点击清除密钥。密钥不会回填网页、写入浏览器存储、进入课堂导出，或传给 Codex 子进程。

接口地址要求 HTTPS，本机 `localhost` / 回环地址可使用 HTTP。后端不跟随 API 重定向，也不自动重试收费请求。使用第三方 API 的费用由该服务商结算，与 ChatGPT / Codex 订阅额度分开。

进入 API 模式或更换 API 模型、地址、密钥时，已有课堂的自动逐段解释会关闭；新建 API 课堂也默认关闭，需要时再手动打开。用量区域仅统计当前课堂的 API 尝试次数及服务商已返回的 token，不估算人民币金额、不设金额硬预算；缺失用量不能当作免费。

切换接入时，正在执行的请求继续使用原来的配置，完成后新配置生效；积压任务暂停，由你选择补分析或从最新继续。历史正式答案保留原接入与模型标记，不会自动切换服务商重答。流式文字在完成校验前是草稿；中断草稿单独标记，不能当作正式答案或后续 AI 的依据。

## 上课时怎么用

1. 双击 `启动课间.vbs`，浏览器打开 [本机界面](http://127.0.0.1:8766)。也可用 PowerShell 执行 `./start.ps1`。
2. 新建一节课，选课程预设，检查运行模式和麦克风，先做一次**十秒试音**并回听。试音不加入课堂记录。
3. 确认课堂允许录音后，手动点击「开始录音」。需要时选择「逐段解释」或「只在提问时分析」，在右栏提问。
4. 结束时点击「暂停录音」，等待尾段上传及转写完成，再导出。AI 失败时可选择补分析或从最新内容继续。
5. 用完双击 `停止课间.vbs`。单独关闭网页不等于关闭后台服务。

手动原文和原文编辑每次最多 **12,000** 个字符，问题最多 **4,000** 个，课程大纲最多 **12,000** 个。前后端按 **Unicode 码点**计数，空白也计入：中英文字一般计 1 个，`😀` 计 1 个，组合 emoji（例如 `👩‍💻`）可能包含多个码点。这不是字节数、UTF-16 长度或 AI token 数。网页从本地服务读取同一套上限；超限会显示字段、上限与当前长度，保留当前页面的输入，不截断内容。必填原文和问题不能仅含空白，大纲可留空。未提交的文字草稿不保证跨刷新保存。

刷新网页保持当前选中的课堂；后台重启沿用“选择最新创建课堂”的规则，不记忆上次手动切换。新课堂在同秒创建时使用持久序号区分先后；旧记录没有该序号时，同秒按课堂 ID 升序确定回退，不改写旧记录或推测真实创建顺序。

首次模型加载、分片等待、队列和 Codex 调用都计入实际等待时间。出现积压时请看页面状态，不要把“已录到音频”理解成“已完成转写”。

## 数据与边界

录音、课堂原文、解释、问答和修订默认保存在本机 `data/`；未发送成功的录音片段暂存在当前浏览器的 IndexedDB。请在离开页面前处理待发送提示。服务默认只监听 `127.0.0.1`。

使用 AI 解释、问答或术语生成时，相关课堂原文、问题、术语或大纲**文字**会发送给当前选择的 Codex 或 API 服务商；原始音频不发送给大模型。Codex 登录由 Codex 管理。仓库不附带账号凭据、个人课程记录或模型权重；分享问题时也请先检查日志、导出文件和截图。

语音模型和 AI 都可能出错，尤其是远距离收音、中英切换、数字、否定词和公式。原文修改后，旧解释和问答会提示依据已变化；已有答案不会悄悄改写。当前不提供板书 / PPT 识别、说话人区分或语音提问。

## 开发检查

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest tests -q
node tests/audio.cjs
node tests/ai-settings.cjs
node tests/input-limits.cjs
node --check static/app.js
node --check static/pcm-worklet.js
```

这些自动化检查使用模拟组件或合成音频，不会开启真实麦克风，也不调用真实 Codex。性能数据、已知识别失败和未验证场景见 [docs/VALIDATION.md](docs/VALIDATION.md)。

## 从 v1.0.0 / v1.1.0 升级

先在网页暂停录音，等待待上传音频、转写和当前 AI 请求结束，再退出后台。备份现有程序目录，更新源码和依赖；保留原 `data/`、`config.json`、`.venv/` 和模型目录。不要用示例配置覆盖自己的模型路径，也不要把个人数据提交 Git。

保持原来的 `http://127.0.0.1:8766` 地址与课堂数据目录。程序会兼容补齐旧记录字段，原回答不自动重算。启动器会核对后台版本，若旧后台仍在运行，会提示先停止旧版本。网页和 `/api/health` 均可核对 `1.1.1`。本补丁不要求修改依赖或批量迁移历史课堂；新课堂多一个可选 `creation_order` 字段，旧版本可忽略它。

回退前先停止新版，另外备份升级后新增的记录，再恢复升级前的程序、数据和配置备份。不要让两个版本同时写同一数据目录；不要用旧数据备份直接覆盖唯一一份升级后新记录。回退到 v1.1.0 后，本补丁修复的限制提示、同秒选择及恢复问题会重新存在。

## 许可

本仓库暂未指定许可证，公开代码不等于授予任意复制、修改或再分发许可。第三方依赖和模型分别适用其自身许可证。
