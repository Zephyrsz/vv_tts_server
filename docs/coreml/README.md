# Core ML／ANE 开发分支

更新：2026-10-25（Asia/Shanghai）。本文件仅在 `feature/coreml-ane` 分支维护。

本项目与 `ai_reader` 均已分离两个本地分支：

| 分支 | 内容 |
|---|---|
| `feature/cpu-baseline` | 从 `main` 的 `d704b51` 保存当前四音色 CPU 服务与历史分析；不引入 Core ML 实现 |
| `feature/coreml-ane` | 保留 CPU 基线，单独推进 [ANE 适配方案](../analysis/2026-10-24-ane-adaptation-optimization.md) 的实现与上游交付 |

当前服务端运行代码与基线相同。本次新增基础组件在相邻 AI Reader 的同名 ANE 分支：帧边界／padding／裁剪、CPU 回退包装器与后端缓存隔离。详见 [AI Reader 分支说明](../../../ai_reader/docs/branches.md)。这些组件尚未接通 Core ML 推理。

## 后续输入及工作

需要上游提供兼容 `vv_bin` 的 Core ML 后端，或获准转换的源模型／Core ML 模型，以及复用 Rust Core 帧展开的原生接口。当前稳定 C ABI 没有可替换 Talk decoder 的帧特征入口。

原生 Core ML 路线后续需补齐：模型包 schema 与内容 hash、style→内部 speaker ID 映射、固定长度桶的 padding 对齐证据、C ABI 交付、模型包校验／安装工具和参考输入输出。当前没有 `tools/coreml/validate_candidate.py` 或可安装的兼容模型包。

不修改保护格式、不声称当前四音色已在 ANE 执行；真正的音质、设备分配、延迟和功耗仍需模型与真机验收。CPU 分支继续保留现有合成路径，`main` 本次不合并、不改写。仅保存本地分支，不推送远程。

本次实际检出 CPU 分支验证：Python 7 项、浏览器播放 6 项通过。服务端代码在两个分支间没有差异，见 [验证记录](verification/2026-10-25-cpu-tests.txt)。
