# 本地推理引擎的来源和复现

Windows 包的 `local-ai/` 与主程序放在同一层。首次点击助手就可以读取这个目录中的模型。默认使用两个 CPU 线程，每次只生成一个诊断主题编号，完成后释放进程和模型内存。主程序负责提供该事件允许的主题、解释文字和不可变的事实。

| 组件 | 固定来源 | 许可证和随包文件 |
| --- | --- | --- |
| Qwen2.5-0.5B-Instruct，GGUF Q4_K_M | [Qwen 官方模型仓库，修订 9217f5d](https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF/tree/9217f5db79a29953eb74d5343926648285ec7e67) | Apache License 2.0，随包提供 `licenses/LICENSE-Qwen` |
| llama.cpp，Windows x64 CPU 版 | [ggml-org 的 b11960 发布](https://github.com/ggml-org/llama.cpp/releases/tag/b11960)，源码提交 `8642541b255bfb5dc39cd351a178be09013a55d6` | MIT，随包提供 `licenses/LICENSE-llama.cpp` |
| OpenMP 和 json.hpp | 来自同一版本的运行库或源码许可 | `licenses/LICENSE-LLVM-OpenMP` 和 `licenses/LICENSE-jsonhpp` |

模型为 **491,400,032 字节**，SHA-256 为 `74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db`。原始 CPU 压缩包的 SHA-256 为 `73f962f82850d244db47eaacb9d913637027b82399124324298576d58c5f4287`。模型、完成程序、运行所需 DLL 和许可证的逐文件大小及摘要在 [asset-lock.json](asset-lock.json) 中。仓库以发布包分发大文件。

在源代码根目录用 Python 3.12 运行以下命令，生成首次使用的本地模型目录。构建脚本会逐项校验来源和输出。

```powershell
python tools/prepare_local_ai.py --cache ../ai-downloads --output ./local-ai
```

准备好缓存后，加 `--offline` 可完全离线组装。缓存中须有清单指定的模型、原始引擎 ZIP 及脚本中列出的三份上游许可证。输出目录必须是新目录，以免覆盖已存在的安装。

排查时可单独检查 `quota_guard.local_ai_runtime.verify_assets(Path("local-ai"))`。模型缺失、DLL 校验不一致、出现额外 DLL 或引擎超时都会使助手使用规则说明。模型仅在用户点击解释时运行。主程序把六个固定分类发送给本地进程，输出经语法约束及主题清单验证后用于调整阅读顺序。
