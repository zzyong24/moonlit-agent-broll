# 来源与修改说明

本仓库是 https://github.com/qwbwj3/agent-broll 的衍生副本。上游许可证为 MIT License，Copyright (c) 2026 qwbwj3。原许可证全文见根目录 `LICENSE`，已原样保留，未改动正文。

本团队版修改（2026）主要包括：默认视频通道改为可配置的本地 muse2api（`providers/muse2api.json`、`scripts/muse.py`、`tests/test_muse.py`）；生图默认改为用户自行配置的本地脚本，备选 Codex；知识科普 B-roll 的风格，以及随仓库分发的默认角色 `characters/final-brick/`。原版秘塔 H3 接入仍作为备选保留。公开副本中的本机绝对路径、私有服务地址和账户标识已改为环境变量或用户配置占位，不随仓库分发。

`characters/final-brick/` 是本衍生副本里团队选用的积木人设定，文件为 `characters/final-brick/front.png`、`characters/final-brick/three_quarter.png`、`characters/final-brick/side.png`、`characters/final-brick/back.png`、`characters/final-brick/expressions.png`、`characters/final-brick/sheet.png`、`characters/final-brick/base.txt`、`characters/final-brick/状态.md`。这些图不是上游 https://github.com/qwbwj3/agent-broll 的示例角色，上游示例不在本仓库中。下载整个 Skill 后，默认以 `characters/final-brick/front.png` 为主身份参考并每期复用；使用者可以换成自己的角色。本段只说明这套素材和上游示例不是同一批图，以及它在本仓库中的默认用法，不另行声明这些图片的法律权属。

本仓库里团队对上游代码和说明文字的修改，版权由月明团队持有。这一句只覆盖这些修改，不作为 `characters/final-brick/` 图片的权属声明。使用、复制与再分发须同时遵守上述 MIT 许可证，包括在所有副本或实质部分中保留上游版权声明与许可声明。
