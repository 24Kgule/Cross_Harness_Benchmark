# Third-party sources

Harness CLIs and runtime dependencies are downloaded by the installer; they are not vendored as executables. Their package licenses and terms apply independently.

- [Codex](https://github.com/openai/codex)
- [Claude Code](https://github.com/anthropics/claude-code)
- [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)
- [OpenCode](https://github.com/anomalyco/opencode)
- [Pi](https://github.com/earendil-works/pi)
- [Kimi Code](https://www.npmjs.com/package/@moonshot-ai/kimi-code)
- [Harbor](https://github.com/laude-institute/harbor)
- [SWE-Bench](https://github.com/SWE-bench/SWE-bench) and its [Harbor dataset adapter](https://github.com/laude-institute/harbor-datasets)
- [Terminal-Bench 2](https://github.com/laude-institute/terminal-bench-2)
- [AppWorld](https://github.com/StonyBrookNLP/appworld)

`configs/datasets/` contains public task identifiers and fixed upstream references. Dataset contents are downloaded separately. Do not redistribute reference solutions, hidden tests or task-specific outputs as training data; follow each dataset's own terms.
