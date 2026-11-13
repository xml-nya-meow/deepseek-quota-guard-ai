# 第三方组件与参考资料

程序使用 Python 标准库（Tk、SQLite、Decimal）、openpyxl、defusedxml、Playwright 和随其安装的 Chromium；构建使用 PyInstaller。构建脚本复制锁定依赖中可取得的许可文件至“第三方许可”，并保留浏览器组件的随附文件。发布前应核查最终便携包内的 Python、Node、Chromium、Tcl/Tk 等组件许可完整。

| 组件 | 锁定版本 | 上游 |
| --- | --- | --- |
| Python | 3.12.14 / 3.13.17，见产物清单 | https://www.python.org/ |
| openpyxl | 3.1.5 | https://openpyxl.readthedocs.io/ |
| defusedxml | 0.7.1 | https://github.com/tiran/defusedxml |
| Playwright | 1.64.1 | https://playwright.dev/python/ |
| PyInstaller（构建） | 6.22.3 | https://pyinstaller.org/ |

直接与传递依赖的完整版本以仓库根目录的 `requirements.lock` 为准。浏览器组件版本从已安装 Playwright 的清单解析；每次便携构建会在 `BUILD-INFO.json` 中记录 Python、依赖与浏览器 revision，不包含构建机器用户名或路径。

本文件记录第三方组件与来源，不替代本项目自身的许可证。本项目自身代码自 `0.1.0rc6.dev11` 起采用 [Apache License 2.0](LICENSE)，署名为 Copyright (c) 2026 xml-nya-meow，见 [NOTICE](NOTICE)。这是项目自身授权的变更，不改变上述第三方组件的许可证；第三方原有 MIT、Apache 或其他许可文本与版权声明均保留。此前已经按 MIT 发布的项目版本仍以其随附许可为准，不追溯改写。

`third_party/` 保存 [Tcl core-8-6-12 原始许可](https://github.com/tcltk/tcl/blob/core-8-6-12/license.terms) 和 [Tcl core-8-6-15 原始许可](https://github.com/tcltk/tcl/blob/core-8-6-15/license.terms)，用于补齐对应运行时可能缺少的许可原文。构建优先使用运行时附带许可，记录实际 Tcl 版本；其他 Tcl 版本缺少许可时会停止构建。

公开资料用于确认边界或参考格式；未把下列项目整套代码移入程序，也未采用其读取浏览器令牌的方式。

- [DeepSeek 余额接口](https://api-docs.deepseek.com/api/get-user-balance/)：返回账户余额，不能据此区分每个人的消费。
- [DeepSeek 平台常见问题](https://static.deepseek.com/faq/index.html?lang=zh)：Key 管理与用量导出入口。
- [deepseek-api-usage-analysis](https://github.com/GavinCnod/deepseek-api-usage-analysis)：参考 CSV 分析方式，不能仅因社区实现存在就认定已核实实际扣费。
- [deepseek-platform-widget 的接口记录](https://github.com/MCwasd/deepseek-platform-widget/blob/main/deepseek-api-reference.md)：参考逐 Key 费用的网页响应结构。该记录属于社区观察，不是官方接口承诺。
- [Playwright 的 PyInstaller 打包说明](https://playwright.dev/python/docs/library#pyinstaller)：自带浏览器与 Python 程序打包。

本程序与 DeepSeek 没有官方隶属关系。依赖版本和网页适配需要后续维护。

## 内置本地 AI 组件：0.5B 诊断

Qwen2.5-0.5B-Instruct Q4_K_M 随附 Apache-2.0；llama.cpp CPU 完成程序随附 MIT；随包也保留 LLVM OpenMP 与 json.hpp 的许可。来源、版本和校验情况见[锁定资源说明](https://github.com/xml-nya-meow/deepseek-quota-guard-ai/tree/v0.2.0a1/local_ai)。便携包的许可原文在 `local-ai/licenses/`。

## 内置本地 AI 组件：4B 网页操作

`0.2.0a3.dev0` 新产品在独立的 `operator-ai/` 目录随附 **Qwen3-4B-Instruct-2507，Q4_K_M**、CPU 推理引擎和官方对话模板。该模型用于受限网页操作，0.5B 诊断模型不是其备用操作员。4B 模型卡说明参数量为 4.0B，属于 non-thinking 版本；本项目没有对权重追加训练，也不把动作 JSON 称为思维链。

以下许可与来源依据本轮干净资产副本中的实际文件记录，未用模型名或量化文件名代替许可核对。

| 组件 | 固定来源与版本 | 随包依据与归属 |
| --- | --- | --- |
| Qwen3-4B-Instruct-2507 基础模型 | [Qwen 固定版本](https://huggingface.co/Qwen/Qwen3-4B-Instruct-2507/tree/cdbee75f17c01a7cc42f958dc650907174af0554) | `operator-ai/metadata/base-README.md` 明列 `license: apache-2.0`；`base-LICENSE` 保留 Apache-2.0 原文。模型归属 Qwen。 |
| Q4_K_M GGUF 量化 | [bartowski 固定版本](https://huggingface.co/bartowski/Qwen_Qwen3-4B-Instruct-2507-GGUF/tree/ae44f08e1392f39c0e474af10c3ff8355c8b6688) | `operator-ai/metadata/quant-README.md` 明列 `quantized_by: bartowski`、基础模型关系与量化说明。该副本没有独立的 license 字段或独立许可原文；这里保留量化作者署名及基座许可，不虚构另一份量化授权。 |
| 官方 Jinja 对话模板 | 与上述 Qwen 基础模型相同的固定版本 | `operator-ai/metadata/qwen3-official.jinja` 是 `base-tokenizer_config.json` 中 `chat_template` 的逐字提取，保留基座 Apache-2.0 许可；不是本项目自创或 GGUF 内置旧模板。 |
| llama.cpp CPU 引擎 | [b11960](https://github.com/ggml-org/llama.cpp/releases/tag/b11960)，[固定提交](https://github.com/ggml-org/llama.cpp/tree/8642541b255bfb5dc39cd351a178be09013a55d6) | `operator-ai/metadata/engine-LICENSE` 为 MIT，版权行是 Copyright (c) 2023-2026 The ggml authors；随附引擎 DLL/EXE 的身份见清单。量化 README 提到的 b6096 是其量化制作版本，不是本包运行引擎版本。 |
| LLVM OpenMP 随附运行库 | 上述引擎发行包中的 `libomp.dll` | `operator-ai/engine/LICENSE-LLVM-OpenMP` 保留原文，文件说明 Apache License v2.0 with LLVM Exceptions，并包含后续许可与版权条款。不得只拷贝普通 Apache 文本代替此文件。 |

正式便携包保留 `operator-ai/asset-lock.json`、其列出的 metadata、引擎文件与原许可。程序和构建按清单校验；本项目的 Apache-2.0 不覆盖或改写第三方许可。基础模型、量化来源与引擎是三个分别锁定的来源，不将第三方 GGUF 冒称为 Qwen 官方发布的量化文件。

本轮固定指纹：

- 资产清单 SHA-256：`f993de2b3e39e0639a3709adfbc54ca7ec66177edc83b6cad0c08f8ef527dd74`。
- GGUF SHA-256：`2fde00ce69dd4899c70d020845e2638353015bba0fdf161b3eb965f2bca4464e`。
- 官方模板 SHA-256：`0ab115e84509e86db2bb05a63edf63778ae7fe624972cdfa982b1e59053d6710`。

2026-11-13 本次文档核对重新计算了干净资产中 11 个 metadata/许可文件的摘要，均与清单一致，并核对模板与 tokenizer 配置中的原字符串完全相同。本段不表示重新训练、独立审核全部第三方源码，或完成真实平台验收；模型整文件与最终成品的校验以相应构建记录为准。
