# Benchmark development

- Keep this repository focused on evaluating one configured model across native harnesses.
- Select datasets, harnesses and sample sizes through JSON configs, not hard-coded cohorts.
- Load credentials only through `experiment_api.config.load_settings()` and repository `.env`; never print or mount actual keys.
- Preserve dataset assertions and distinguish missing grading from an executed failure.
- Never retry a valid failure automatically. Freeze config/model/code and record extra executions explicitly.
- Keep runtime outputs, downloaded tasks, private state and model results out of source control.
- Use local mocks, synthetic plotting inputs and NOP checks for code verification. Actual model experiments incur usage and must have an explicit experiment request.
