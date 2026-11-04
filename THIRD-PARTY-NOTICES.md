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

## 内置本地 AI 组件

Qwen2.5-0.5B-Instruct Q4_K_M 随附 Apache-2.0；llama.cpp CPU 完成程序随附 MIT；随包也保留 LLVM OpenMP 与 json.hpp 的许可。来源、版本和校验情况见[锁定资源说明](https://github.com/xml-nya-meow/deepseek-quota-guard-ai/tree/v0.2.0a1/local_ai)。便携包的许可原文在 `local-ai/licenses/`。
