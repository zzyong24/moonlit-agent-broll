---
name: moonlit-agent-broll
description: 根据中文口播稿或同版 SRT 选出值得演绎的段落，准备用户角色、图片故事板与干净首帧，经列价确认后用视频模型生成、恢复下载并交付 B-roll；适用于多镜头规划、明确单镜头制作和已有任务恢复。
---

# moonlit-agent-broll

把口播中值得演的关系和变化变成可用镜头。整稿或同版 SRT 从选段开始；明确单镜头只核对已给要求，不强索整稿；修改／恢复先读已有决定和任务记录，只处理本次变化。用户只要规划时，做到规划交付，不推定生成授权，也不要求先提供 Key。

## 先确认工作位置

- 读取用户自己的配置目录，确认所选模型，以及使用者是否已换成自己的角色；首次使用问三件事：**用哪家视频模型、Key 从哪里读取、这次预算多少及币种**。月明团队默认：视频走本机 muse2api（`providers/muse2api.json` + `scripts/muse.py`；Key 只从环境变量 `MUSE2API_KEY` 读取，或由用户通过 `--env-file` 指定自己的文件；服务地址与额度账户须由用户在该 JSON 中自行填写，免费档按周额度），生图默认走用户配置的脚本（如 dudu / gpt-image-2），固定角色默认用 skill 内 `characters/final-brick/`，主参考 `characters/final-brick/front.png`，见下文「月明团队默认」；秘塔 H3（`h3.py`）保留为备选；其他模型按[接入别家模型](references/接入别家模型.md)准备。
- Key 只从环境变量或用户指定的位置读取，不索要用户把真实 Key 贴进聊天，不写进提示词、记录、日志或终端输出。
- 默认角色在 skill 内 `characters/final-brick/`，路径相对 skill 根目录（`SKILL.md` 所在目录）。使用者要换成自己的角色时，把替换后的角色库放在自己的配置目录；未替换就用仓库内这套。配置目录还保存所选模型及 Key 的读取位置。项目目录下的 `.agent-broll/` 保存本片的选段、临时角色、故事板、首帧、报价、确认、请求、任务编号及结果。**skill 安装目录不写运行数据，也不改写 `characters/final-brick/` 里的原图**。
- 修改／恢复先读本项目已有决定和任务记录；已有远端任务编号优先恢复原任务，不能用新提交替代查询或下载。

## 1. 通读整稿，选段交用户确认

按[选段与演绎](references/选段与演绎.md)通读上下文，不逐条字幕机械配图。给用户一份紧凑的选段清单，每段含：

- 可回到原稿的原文锚点；SRT 有真实时间才写时间，纯文本不虚构秒点。
- 为什么值得演，观众要看懂或感受到什么。
- 一句话事件：**起始状态 → 可见变化 → 结束状态**。
- 哪些段落不演及原因；准确事实所需的真实素材或后期工作。

保存确认后的范围。用户要求改选段，就先改选段；不要带着未批准的镜头继续做整批故事板。

## 2. 选择角色，或建立本片临时角色卡

默认使用本 skill 自带的 `characters/final-brick/`，每期复用，不另造主角。主身份参考是 `characters/final-brick/front.png`，生图 `--image` 优先传它。使用者明确要换角色时，再按[角色](references/角色.md)替换：上传自己的形象／用已有图片生成／口述转文生图。未要求替换，就不要另做一套主角。

默认设定已经在 `characters/final-brick/`（`characters/final-brick/front.png`、`characters/final-brick/three_quarter.png`、`characters/final-brick/side.png`、`characters/final-brick/back.png`、`characters/final-brick/expressions.png`、`characters/final-brick/sheet.png` 和 `characters/final-brick/base.txt`）。只有换成新主角时才另做多角度、表情、动作和干净全身图，并附外观、材质、比例和禁止项。仍须保证片内一致，仍须过故事板关卡。不要使用第三方 IP 或未经同意的他人照片。

## 3. 先定第一格风格，再审整页图片故事板

按[故事板](references/故事板.md)执行：

1. 默认用 **用户配置的生图脚本（如 dudu / gpt-image-2）** 出第一格，命令由用户配置目录提供，例如：`python3 <用户配置的生图脚本> --prompt-file <提示词.txt> --out <项目>/.agent-broll/boards/<镜头>.png --size 1536x1024 --model gpt-image-2 --image characters/final-brick/front.png`。这里的 `characters/final-brick/front.png` 相对 skill 根目录；`--size 1536x1024` 只用于 B-roll 首帧。月明已批准的备选是 Codex 内置生图。让用户定风格；不宣称生图免费或额度无限，脚本单价未记录，批量出图前说明张数。
2. 第一格认可后出整页。每格配原文锚点、事件变化、出场角色、镜头说明；制作注记放画格外。
3. Agent 先做去字测试和静音测试，写出每个有疑问镜头如何成立或如何修改，再交用户审改。
4. 用户通过本批完整故事板后，才进入视频生成。风格通过不等于整批通过，故事板通过不等于费用确认。

**多镜头任务不得跳过这一步。** 唯一例外是用户已给出明确单个镜头和生成参数；只补齐会影响画面或报价的缺项，不扩成整片规划。宿主没有生图能力时，请用户配置图片模型或手动导入图片故事板，不能直接跳到视频。第三方图片生成如收费，同样先独立列价确认。

## 4. 单独生成干净首帧

从通过的故事板格重新生成一张干净起始画面，去掉格框、文字、镜头编号和制作注记；检查身份、构图、动作起点与批准方案相符。**故事板网格不得直接上传给视频模型**。

按[提示词](references/提示词.md)分开写生图与视频提示词，区分参考图的身份、风格、构图和首帧用途。文件路径、内部编号和审批说明留在项目记录，不进生成提示词。只把本次用户指定用于生成的素材传给所选服务；仅供看图或规划的资料不默认上传。

## 5. 列价 → 确认 → 提交 → 查询 → 下载

先读[价格与提交](references/价格与提交.md)，默认用 `providers/muse2api.json` 和 `scripts/muse.py` 记录、确认、执行（提交前自动实时查周额度，≥95% 拒绝提交）；选秘塔时用 `providers/metaso_h3.json` 和 `scripts/h3.py`，不凭记忆给通用单价。

- 默认将本批所有镜头**逐条列价、合计后确认一次**；用户也可选择逐条确认。报价含模型、模式、时长、分辨率、各计费项、本次金额及剩余预算。关键计费规则或兑换基准未知时先核实，不能猜完整报价；取整、账户档位等已列明的未知项须由用户明确确认，Agent 不能代批。付费提示词优化默认关闭，本版脚本不实现开启该项。
- 每次提交前仍展示该次金额和剩余预算。只在已确认批次、请求未变且未超预算的正常提交中免重复询问。预算上限或“故事板通过”不能代替报价确认。
- **重做、改参数、换模型、开启附加收费项都重新报价确认**，保留旧结果。不得因还有预算就自动继续生成。
- 使用脚本先持久化“即将提交”的请求与估价，提交一次，拿到任务编号立刻保存。没有确认过的报价，脚本应拒绝提交。
- 超时、断网或未拿到可靠编号时标“提交结果不明”；能查原任务就查，查不到停下来告知用户，不能自动重投。已有编号时只恢复查询或下载。进程中断后同样先查看记录。
- 下载结果不携带 Key。HTTP 200 不代表生成成功，下载到空文件或网页错误页不能交付为视频。

## 6. 检查并交付真实结果

交付本地文件，并逐条说明对应原文锚点、请求时长、实际文件时长、估价与实际费用、检查状态及使用建议。

- 读文件／媒体信息，确认不是空文件或网页；未能读取实际时长就写“实际时长未核实”。
- 只抽帧就写“仅抽帧检查”，不能写“已完整审看”；连续观看过才报告连续观看的结论。未听音频就写“未听审”，不要把预览静音当成原文件无声。
- 实际费用以供应商可靠扣费信息为据；拿不到就写“实际费用未核实”，单独保留估价，不能用估价冒充已扣费用。
- 写清已交付、仍在排队、生成失败、下载失败或提交结果不明的项，不把计划、截图自查或技术检查当作用户接受。

## 月明团队默认

- **风格**：知识科普类 B-roll 一律用积木人场景（光泽塑料积木小人 + 积木搭成的场景道具），搭配搞怪表情（大笑、震惊、挤眼、得意举拳等）；**不用写实风**，画面里不出现任何商标、品牌标识或可读文字。
- **固定角色**：默认目录 `characters/final-brick/`（相对 skill 根）。`characters/final-brick/front.png` 是主身份参考，生图 `--image` 优先传它；同目录还有 `characters/final-brick/three_quarter.png`、`characters/final-brick/side.png`、`characters/final-brick/back.png`、`characters/final-brick/expressions.png`、`characters/final-brick/sheet.png`，外观描述在 `characters/final-brick/base.txt`：积木头、蓬松黑色短发、笑脸、藏青色西装外套、白色T恤、深色腿、黑色底座脚、光泽塑料质感。每期复用这一套，不另造主角。使用者可以换成自己的角色；未替换时读取上述相对路径。设定图保持原比例：`characters/final-brick/front.png`、`characters/final-brick/three_quarter.png`、`characters/final-brick/side.png`、`characters/final-brick/back.png`、`characters/final-brick/expressions.png` 为 1024×1536，`characters/final-brick/sheet.png` 为 4096×1536。16:9 不约束这些设定图，也不把它们裁成横屏。
- **生图**：默认使用用户配置的生图脚本（如 dudu / gpt-image-2），Key 由该脚本自己读，不打印；备选 Codex（月明已批准用于 B-roll 生图）。**月明的 B-roll 镜头都是横屏：一律 16:9，不做竖屏**，镜头首帧固定 `--size 1536x1024`。16:9 只约束 B-roll 镜头和首帧，不约束 `characters/final-brick/` 里的设定图。
- **视频**：`scripts/muse.py`，mode 用 `first_frame`（首帧放 `images[0]`，role `first_frame`）或 `text_to_video`；`ratio` 默认且一律用 `16:9`（省略时 muse.py 自动填 16:9；不做 9:16 竖屏）；`duration` 4–10 秒整数（只是提示，实际时长以 ffprobe 为准）；`resolution` 可省略（720P）。使用者自行启动 muse2api 或等效服务，并在 `providers/muse2api.json` 填写 `api.base_url` 与 `api.quota_account_id`；本仓库不内置具体地址、端口或账户标识。
- **交付检查**：下载后 `ffprobe` 读时长、分辨率、音轨，再抽帧目检；写明“仅抽帧检查”“未听审”等真实状态。
- 运行数据放项目目录 `.agent-broll/`，不写进 skill 目录。
