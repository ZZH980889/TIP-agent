# Installation and Execution Commands

Replace every `/path/to/...`, `YOUR_API_KEY`, and `YOUR_MODEL_ID` value below. Unless a command changes directories explicitly, start from the repository root.

## 1. Configure the model endpoint

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

The scripts default to an output-token limit of 4096 for ALFWorld/WebShop and 8192 for SQL. To override this, use `export OPENAI_MAX_TOKENS=4096` in Bash or `$env:OPENAI_MAX_TOKENS="4096"` in PowerShell.

ALFWorld/WebShop read environment variables and do not automatically load a `.env` file. SQL also accepts `--api-env /path/to/config` and reads `.api_env` in its working directory. Do not commit credential files.

Leave `USE_PROXY=0` unless the SQL client's configured local proxy is available.

## 2. ALFWorld

Install the agent dependencies and prepare official environment data:

```bash
python -m pip install alfworld openai openpyxl PyYAML
```

### Linux / WSL

```bash
export ALFWORLD_DATA="/path/to/alfworld-data"
alfworld-download
cd alfworld
python run_tip_agent.py
```

### PowerShell

```powershell
$env:ALFWORLD_DATA = "D:/datasets/alfworld"
alfworld-download
Set-Location alfworld
python run_tip_agent.py
```

If data is already installed, omit `alfworld-download` and set `ALFWORLD_DATA` to that installation's data root. It must contain the `json_2.1.1` and `logic` directories.

Run the agent from `alfworld/` because it reads `base_config.yaml` and `prompts/alfworld_3prompts.json` using relative paths. The entry point is named `run_tip_agent.py` to avoid shadowing the installed `alfworld` package.

## 3. WebShop

Install the official environment, its dependencies, product data, and indexes first. Then install the direct agent dependencies:

```bash
python -m pip install openai openpyxl numpy
```

### Linux / WSL

```bash
export WSPRO_SEED=233
export WEBSHOP_NUM_EPISODES=500
export WEBSHOP_MAX_STEPS=50
export PYTHONPATH="/path/to/WebShop:${PYTHONPATH:-}"
cd /path/to/WebShop
python /path/to/TIP-agent/webshop/run_tip_agent.py
```

### PowerShell

```powershell
$env:WSPRO_SEED = "233"
$env:WEBSHOP_NUM_EPISODES = "500"
$env:WEBSHOP_MAX_STEPS = "50"
$env:PYTHONPATH = "D:/path/to/WebShop;$env:PYTHONPATH"
Set-Location "D:/path/to/WebShop"
python "D:/path/to/TIP-agent/webshop/run_tip_agent.py"
```

Use the WebShop root as the working directory so its resource paths resolve correctly. Outputs are written to the working directory. This is a local-environment implementation; it does not require a separate HTTP server.

For a short run, set `WEBSHOP_NUM_EPISODES=1`.

## 4. InterCode-SQL

Install dependencies:

```bash
python -m pip install requests scipy tiktoken
```

Check the command-line interface from the repository root:

```bash
python intercode_sql/run_tip_agent.py --help
```

### Linux / WSL single-task run

```bash
python intercode_sql/run_tip_agent.py --data-path /path/to/tasks.json --sqlite-root /path/to/databases --task-index 0 --limit 1 --max-turns 10 --candidates-k 5 --log-path sql_smoke.jsonl --work-root sql_work
```

### PowerShell single-task run

```powershell
python intercode_sql/run_tip_agent.py --data-path "D:/datasets/sql/tasks.json" --sqlite-root "D:/datasets/sql/databases" --task-index 0 --limit 1 --max-turns 10 --candidates-k 5 --log-path sql_smoke.jsonl --work-root sql_work
```

Task JSON and database paths must refer to your actual data. The JSON format and supported database layouts are described in [README.md](README.md).

For full evaluation, change `--limit` to the intended task count and verify that the range beginning at `--task-index` is valid. The multi-task branch supports `--best-of`, defaulting to 1. Best-of selection is not the same as averaging independent runs.

Keep the included `sqlite_sql/` directory next to the SQL entry point.

## 5. Save and compare runs

Preserve each run's output under a distinct name or working directory. Default workbook filenames are reused and can overwrite previous outputs.

When comparing runs, keep the tasks, model settings, dependencies, and environment configuration controlled and record any intended changes. API retries and per-task best-of attempts are distinct from independent full evaluation runs.
