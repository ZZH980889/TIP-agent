# TIP-Agent

TIP-Agent is an LLM-based agent framework for interactive tasks in **ALFWorld**, **WebShop**, and **InterCode-SQL**. This repository provides an implementation for each environment, together with configuration files, prompt examples, dependency information, and execution commands.

The framework combines three mechanisms:

- **Intent-direct execution:** execute a reliably grounded intended action without additional candidate-level planning.
- **Progress-guided planning:** use transition evidence and task-progress assessment to select an action when direct execution is unavailable. The environment adapters use predicted transitions or real probes as appropriate.
- **Runtime memory and pruning:** use observed transitions to filter unproductive choices, prioritize candidates, and recover from repeated action patterns.

The implementations use an OpenAI-compatible API. You supply the model endpoint, credentials, and environment data; no local LLM training step is required.

## Supported environments

| Environment | Task | Implementation |
|---|---|---|
| ALFWorld | Text-based household task execution | ALFWorld TextWorld environment, legal-action grounding, and few-shot prompts |
| WebShop | Product search, option selection, and purchase | Local WebShop text environment with product probing |
| InterCode-SQL | Interactive SQL querying with execution feedback | Included SQLite adapter with schema inspection and result-based scoring |

## Repository structure

```text
TIP-agent/
├── README.md
├── DEPENDENCIES.md
├── RUN_COMMANDS.md
├── alfworld/
│   ├── run_tip_agent.py
│   ├── base_config.yaml
│   └── prompts/
│       └── alfworld_3prompts.json
├── webshop/
│   └── run_tip_agent.py
└── intercode_sql/
    ├── run_tip_agent.py
    └── sqlite_sql/
        ├── __init__.py
        ├── env.py
        ├── mysql_compat.py
        └── reward.py
```

Keep each environment's supporting files in the shown locations. In particular, the SQL entry point imports the adjacent `sqlite_sql` package, and ALFWorld loads its configuration and prompts from relative paths.

## 1. Get the code

```bash
git clone https://github.com/ZZH980889/TIP-agent.git
cd TIP-agent
```

You can also download the repository as a ZIP archive and extract it. For a private repository, cloning requires an account with repository access.

Use a separate Python environment for each benchmark so that its dependencies can be installed independently. Select the Python version and system dependencies required by the benchmark version you are using. [DEPENDENCIES.md](DEPENDENCIES.md) lists the direct dependencies; the external environments also have their own installation requirements.

Benchmark data, product indexes, and SQL databases are not bundled with this repository.

## 2. Configure the model API

Set the following variables in the same terminal used to launch an experiment.

### Linux / WSL / Bash

```bash
export OPENAI_API_KEY="YOUR_API_KEY"
export OPENAI_BASE_URL="https://YOUR_PROVIDER/v1"
export OPENAI_MODEL="YOUR_MODEL_ID"
export USE_PROXY=0
```

### Windows PowerShell

```powershell
$env:OPENAI_API_KEY = "YOUR_API_KEY"
$env:OPENAI_BASE_URL = "https://YOUR_PROVIDER/v1"
$env:OPENAI_MODEL = "YOUR_MODEL_ID"
$env:USE_PROXY = "0"
```

Replace all placeholder values with your provider's settings. The provider must support the chat-completions interface used by the selected script.

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY` | Your API credential |
| `OPENAI_BASE_URL` | OpenAI-compatible endpoint; explicitly set this to your provider |
| `OPENAI_MODEL` | Provider-specific model identifier |
| `OPENAI_MAX_TOKENS` | Output-token limit; defaults to 4096 for ALFWorld/WebShop and 8192 for SQL |
| `USE_PROXY` | SQL proxy switch; defaults to 0 |

For example, to set a 4096-token output limit in Bash:

```bash
export OPENAI_MAX_TOKENS=4096
```

ALFWorld and WebShop read shell environment variables; they do not automatically load a `.env` file. SQL also supports `--api-env /path/to/config` and reads `.api_env` in its current working directory. Keep credential files outside version control.

Leave `USE_PROXY=0` unless you have configured the proxy expected by the SQL client. Its enabled proxy address is currently `http://127.0.0.1:7897`.

## 3. Run ALFWorld

### Install dependencies and prepare data

Install the [official ALFWorld environment](https://github.com/alfworld/alfworld) and its required dependencies. The agent directly uses:

```bash
python -m pip install alfworld openai openpyxl PyYAML
```

Set the data location and download the environment resources:

```bash
export ALFWORLD_DATA="/absolute/path/to/alfworld-data"
alfworld-download
```

The configured data directory must contain the `json_2.1.1` and `logic` resources referenced by `base_config.yaml`. For an existing installation, point `ALFWORLD_DATA` to its data directory rather than downloading another copy.

### Execute

From the repository root:

```bash
cd alfworld
python run_tip_agent.py
```

Run from the `alfworld/` directory so that `base_config.yaml` and `prompts/alfworld_3prompts.json` resolve correctly.

The entry point evaluates 134 tasks from the out-of-distribution split, using a default limit of 50 steps per task. It loads task-type-specific few-shot prompts from the JSON file. To change prompt content, edit the corresponding entries in that file. Environment paths and settings are configured in `base_config.yaml`.

### Output

The console reports task progress, rewards, success counts, and LLM call statistics. The script writes:

```text
test4_wm_universal_results-1step.xlsx
```

The workbook contains aggregate and task-type success statistics and LLM call information. Move or rename it before another run if you want to retain the previous results.

## 4. Run WebShop

### Install the environment

Set up [the official WebShop repository](https://github.com/princeton-nlp/WebShop), including its Python/system dependencies, product data, and search indexes.

Install the agent's direct dependencies:

```bash
python -m pip install openai openpyxl numpy
```

This implementation uses `web_agent_site.envs.WebAgentTextEnv` locally. It is not an HTTP client for a separately hosted WebShop server.

### Execute

Use absolute paths for the two repositories:

```bash
export PYTHONPATH="/absolute/path/to/WebShop:${PYTHONPATH:-}"
export WSPRO_SEED=233
export WEBSHOP_NUM_EPISODES=500
export WEBSHOP_MAX_STEPS=50

cd /absolute/path/to/WebShop
python /absolute/path/to/TIP-agent/webshop/run_tip_agent.py
```

Launching from the WebShop root lets the upstream environment resolve its local resources.

| Variable | Default | Purpose |
|---|---:|---|
| `WSPRO_SEED` | 233 | Seed set before environment construction |
| `WEBSHOP_NUM_EPISODES` | 500 | Number of evaluation sessions |
| `WEBSHOP_MAX_STEPS` | 50 | Maximum steps per session |
| `WSPRO_MODE` | PRO | Execution mode selected by the script |
| `WEBSHOP_NUM_PRODUCTS` | Unset | Optional product-count argument passed to WebShop |

For a short run, set `WEBSHOP_NUM_EPISODES=1` before launching. Changing the product count changes the environment configuration and may affect comparability across runs.

### Output

The script reports per-session results and writes an Excel workbook to the working directory. With the default settings, its name is:

```text
webshop_pro_results_V4DIAG_PRO_500.xlsx
```

The workbook includes reward/success summaries, cost statistics, and diagnostic records collected by the implementation. Preserve each run's output separately.

## 5. Run InterCode-SQL

### Install dependencies

```bash
python -m pip install requests scipy tiktoken
```

The repository includes the required `sqlite_sql` adapter. Do not install an unrelated package with that name. Python's `sqlite3` module provides the database engine.

This adapter executes queries against temporary copies of SQLite databases. It supports inspection commands such as `SHOW TABLES` and `DESC table` through the supplied compatibility layer. It is distinct from the official InterCode runtime.

### Prepare tasks and databases

The task file is a JSON array. Each record provides:

| Field | Meaning |
|---|---|
| `query` | Natural-language task |
| `db` | Database identifier |
| `gold` | Reference SQL used for scoring |

An illustrative record looks like this:

```json
[
  {
    "query": "List the names of all students.",
    "db": "school",
    "gold": "SELECT name FROM students"
  }
]
```

This example describes the file format; you must supply a matching database with the referenced table.

Supported database layouts are:

```text
databases/
└── school/
    └── school.sqlite
```

or `databases/school.sqlite`. The same layouts are supported with a `.db` extension.

### Execute a single task

From the repository root:

```bash
python intercode_sql/run_tip_agent.py \
  --data-path /absolute/path/to/tasks.json \
  --sqlite-root /absolute/path/to/databases \
  --task-index 0 \
  --limit 1 \
  --max-turns 10 \
  --candidates-k 5 \
  --log-path sql_smoke.jsonl \
  --work-root sql_work
```

For Windows PowerShell, use the equivalent single-line command:

```powershell
python intercode_sql/run_tip_agent.py --data-path "D:/data/tasks.json" --sqlite-root "D:/data/databases" --task-index 0 --limit 1 --max-turns 10 --candidates-k 5 --log-path sql_smoke.jsonl --work-root sql_work
```

### Main command-line options

| Option | Default | Purpose |
|---|---|---|
| `--data-path` | Required | Task JSON file |
| `--sqlite-root` | Required | Database root directory |
| `--task-index` | 0 | Starting task index |
| `--limit` | 1 | Number of tasks |
| `--max-turns` | 10 | Maximum interaction rounds per task |
| `--candidates-k` | 5 | Candidate-count parameter for fallback planning |
| `--log-path` | Unset | Result/trajectory log destination |
| `--work-root` | System temporary location | Parent directory for temporary database copies |
| `--api-env` | Unset | Optional API configuration file |
| `--best-of` | 1 | Attempts per task in the multi-task evaluation branch; retain the best result |

To evaluate a range of tasks, increase `--limit` and ensure that the selected range exists in the task file. The single-task branch does not apply `--best-of`. Best-of selection is different from averaging independent runs.

To view all available options:

```bash
python intercode_sql/run_tip_agent.py --help
```

### Scoring and token accounting

`reward.py` compares the returned rows with the reference-query result using multiset overlap and, when available, a Kendall rank-correlation term. Install SciPy consistently across compared runs: without it, the rank-correlation branch is skipped.

The SQL script uses `tiktoken` when available and falls back to a character-based token estimate otherwise. Keep this dependency consistent when comparing token counts.

## 6. Troubleshooting

| Issue | What to check |
|---|---|
| API-key error | Set `OPENAI_API_KEY` in the terminal running the script |
| Model not found or API error | Check the endpoint, model ID, permissions, and provider compatibility |
| ALFWorld configuration/prompt not found | Run from `alfworld/` and keep the supporting files in place |
| ALFWorld data not found | Check `ALFWORLD_DATA` and the paths in `base_config.yaml` |
| `No module named web_agent_site` | Install WebShop and add its repository root to `PYTHONPATH` |
| WebShop resource/index error | Complete upstream data/index setup and run from the WebShop root |
| `No module named sqlite_sql` | Keep `sqlite_sql/` next to the SQL entry point and execute the provided entry point |
| SQL database not found | Match each task's `db` field to a supported database path |
| SQL score differs across machines | Check data, query results, SciPy availability/version, and other environment settings |
| Previous output disappeared | Rename outputs between runs; default workbook names are reused |

## Further documentation

- [Dependency list](DEPENDENCIES.md)
- [Additional execution commands](RUN_COMMANDS.md)
- [ALFWorld](https://github.com/alfworld/alfworld)
- [WebShop](https://github.com/princeton-nlp/WebShop)
- [InterCode benchmark](https://github.com/princeton-nlp/intercode)
