# Dependencies

Install each benchmark in a separate Python environment compatible with that benchmark's version. The tables below list the direct dependencies of the agent scripts. External environments have additional Python, system, and data requirements; follow their installation instructions as well.

## ALFWorld

| Package or component | Purpose |
|---|---|
| `alfworld` | Text environment, including `AlfredTWEnv` |
| `openai` | OpenAI-compatible model calls; requires a version exposing the `OpenAI` client |
| `openpyxl` | Excel output |
| `PyYAML` | Loading `base_config.yaml` |

```bash
python -m pip install alfworld openai openpyxl PyYAML
```

Prepare the game data and logic files using the [ALFWorld installation instructions](https://github.com/alfworld/alfworld). Set `ALFWORLD_DATA` to the data root containing `json_2.1.1` and `logic`.

## WebShop

| Package or component | Purpose |
|---|---|
| Official WebShop environment and its dependencies | Provides `web_agent_site.envs.WebAgentTextEnv` |
| `openai` | OpenAI-compatible model calls |
| `openpyxl` | Results and diagnostic workbooks |
| `numpy` | Random-seed configuration |

```bash
python -m pip install openai openpyxl numpy
```

Install the [official WebShop environment](https://github.com/princeton-nlp/WebShop), its product data, search indexes, and required system components separately. Installing only the three packages above does not provide a working WebShop environment.

## InterCode-SQL

| Package or component | Purpose |
|---|---|
| `requests` | HTTP calls to the model API |
| `scipy` | Kendall rank-correlation term in the reward function |
| `tiktoken` | Token counting |
| Included `sqlite_sql` package | Environment, schema-probe translation, and reward computation |
| `sqlite3` from the Python standard library | Database execution; no pip installation required |

```bash
python -m pip install requests scipy tiktoken
```

Keep SciPy available and version-consistent across compared runs. Without it, the code skips the rank-correlation term, which can change scores. Without tiktoken, token counting falls back to a character-based estimate.

The included `sqlite_sql` package is a local adapter; do not install an unrelated pip package with that name. This SQLite implementation is distinct from the official [InterCode runtime](https://github.com/princeton-nlp/intercode).

## Model service and data

All three agents require credentials for an OpenAI-compatible model endpoint. Benchmark data, SQL databases, model credentials, and virtual environments are not included.

These dependency lists are not pinned environment lockfiles. For reproducible runs, record the installed versions, Python version, benchmark commits, dataset versions, model ID, and API settings used for the experiment.
