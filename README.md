# moonlit-agent-broll

本仓库是 https://github.com/qwbwj3/agent-broll 的衍生副本。上游许可证为 MIT License，Copyright (c) 2026 qwbwj3；原许可证全文见根目录 `LICENSE`（正文未改）。来源与修改说明见 `NOTICE.md`。

团队版相对上游的最小改动：默认视频通道改为可配置的本地 muse2api（`scripts/muse.py`、`providers/muse2api.json`）；生图默认使用用户自行配置的脚本（如 dudu / gpt-image-2），备选 Codex；知识科普 B-roll 默认积木人风格与搞怪表情，不用写实、无商标。默认主角在 `characters/final-brick/`，主参考是 `characters/final-brick/front.png`，每期复用。原版秘塔 H3 接入仍作为备选保留。本公开副本不包含本机绝对路径、私有服务地址或账户标识；这些项由使用者在本地配置。

把口播视频的字幕交给你的 AI Agent：它挑出值得用角色演的段落，写成分镜，出图给你审；你确认图和报价后，它再调用视频模型生成动画，下载到本地。全程不用写代码，也不用自己写视频提示词。

## 需要准备什么

- **一个能生图的 AI Agent。** 能读写本地文件、能运行 Python、自带生图能力的都可以。作者实际用的是 Codex（内置生图）。Agent 没有生图能力时，可以接一个图片模型，或者你自己把故事板图片放进去。
- **一个视频生成模型的 API Key，并按你的服务准备额度或费用。** 本团队版默认走可配置的本地 muse2api（先在 `providers/muse2api.json` 填写服务地址与额度账户标识，Key 放入环境变量 `MUSE2API_KEY`）。也已接好秘塔 H3（能力、单价和来源见 `providers/metaso_h3.json`）。想用别家，按 `references/接入别家模型.md` 让 Agent 照着官方文档接。
- **这期视频的 SRT 字幕**（或完整口播稿）。
- **一个项目文件夹，和这次的预算。**

## 怎么装

把整个 `skill` 文件夹交给你的 Agent，跟它说：**“帮我安装上这个 skill。”**

不同 Agent 放 skill 的位置不一样（例如 Codex 是 `~/.codex/skills/`），由它按自己的规则放好。

## 第一次使用会问你三件事

1. **用哪家的视频模型？** 本团队版默认走可配置的 muse2api；也可直接用秘塔 H3。
2. **Key 放在哪？** 放进环境变量，或你指定的一个文件。**不要把 Key 贴进聊天**，它也不会被写进日志和项目记录。
3. **这次预算多少？** 预算只是上限，每次生成前仍会先列价，你同意了才提交。

## 怎么用

> 用 moonlit-agent-broll 处理这份字幕。先告诉我哪些地方适合插动画、为什么，用我的形象做故事板；故事板通过后再把整批报价给我确认。

依次要你确认四样：选段 → 角色和第一格的风格 → 整页故事板 → 报价。图不满意就在出图这一步改，改图不花视频生成的钱。

**默认形象已经在仓库里。** 使用 `characters/final-brick/` 这套积木人，`characters/final-brick/front.png` 是主参考，每期复用，不另造主角。16:9 只用于 B-roll 镜头；设定图保持原尺寸。想换成自己的形象时，再用照片或口述另做一套，并自行替换这套默认角色。

**生成失败或下载中断？** 跟 Agent 说“查看原任务并继续查询／下载”。已有任务编号就沿原任务恢复，提交结果不明时会停下来核实，不会自动再扣一次钱。任何重做、改参数、换模型都重新报价。

## 会交付什么

本地视频文件，以及每段对应的字幕位置、实际时长、估价、能核实的实际费用和检查状态。只抽帧没连续看、没听音频，都会如实写明。

准确的数字、软件界面、新闻事实，仍然用真实素材或后期做，不拿生成画面充当证据。

## 费用参考

以秘塔 H3 为例（2026-10 官方页面标价，以你账户实际扣费为准）：768P 约 0.09 元/秒，2K 约 0.15 元/秒。一段 5 秒的 768P 动画约 0.45 元。muse2api 免费档按周额度计，占用比例以你的账户实际为准。

## 文件说明

- `SKILL.md`：给 Agent 读的运行说明
- `NOTICE.md`：上游来源、MIT 声明与团队修改摘要
- `characters/final-brick/`：默认积木人设定。`characters/final-brick/front.png` 为主身份参考，另有 `characters/final-brick/three_quarter.png`、`characters/final-brick/side.png`、`characters/final-brick/back.png`、`characters/final-brick/expressions.png`、`characters/final-brick/sheet.png`、`characters/final-brick/base.txt`、`characters/final-brick/状态.md`。使用者可以替换，未替换时用这套
- `references/`：选段、角色、故事板、提示词、价格与提交、接入别家模型
- `providers/metaso_h3.json`：秘塔 H3 的能力与计价
- `providers/muse2api.json`、`scripts/muse.py`：muse2api 接入（团队默认；公开副本中地址与账户标识留空，由使用者填写）
- `tests/test_muse.py`：muse.py 假服务测试（其中的 Key 仅为测试夹具 fixture，不是真实密钥）
- `scripts/h3.py`：报价、确认、提交、查询、下载（只用 Python 标准库，无需另装依赖）
- `tests/`：本地假服务器测试，`python3 -m unittest discover -s tests`
