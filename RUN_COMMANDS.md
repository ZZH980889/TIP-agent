# 安装与执行语句

下列 /path/to/...、YOUR_KEY 和 YOUR_MODEL_ID 都要替换。命令中的路径以“发布”文件夹为根目录；建议在已配置好的 Linux/WSL 实验环境运行。尚未完成完整环境测试。

## 1. 设置模型接口

Bash（Linux/WSL）：

```bash
export OPENAI_API_KEY="YOUR_KEY"
export OPENAI_BASE_URL="https://YOUR_PROVIDER/v1"
export OPENAI_MODEL="YOUR_MODEL_ID"
export USE_PROXY=0
```

PowerShell：

```powershell
$env:OPENAI_API_KEY = "YOUR_KEY"
$env:OPENAI_BASE_URL = "https://YOUR_PROVIDER/v1"
$env:OPENAI_MODEL = "YOUR_MODEL_ID"
$env:USE_PROXY = "0"
```

不设置 OPENAI_MAX_TOKENS 时，ALFWorld/WebShop 默认 4096，SQL 默认 8192。需要统一上限时，Bash 用 `export OPENAI_MAX_TOKENS=4096`，PowerShell 用 `$env:OPENAI_MAX_TOKENS="4096"`。这些是代码配置方式，不代表任意模型/提供商都兼容。

ALFWorld/WebShop 不会自动读取 .env。SQL 额外支持 --api-env 指定键值配置文件，也会读取当前工作目录的 .api_env；请勿上传这些文件的真实内容。

## 2. ALFWorld

先安装直接依赖，并按官方环境说明完成数据准备：

```bash
python -m pip install alfworld openai openpyxl PyYAML
alfworld-download
```

Bash，从“发布”根目录执行：

```bash
export ALFWORLD_DATA="/path/to/alfworld-data"
cd alfworld
python run_tip_agent.py
```

PowerShell，从“发布”根目录执行：

```powershell
$env:ALFWORLD_DATA = "D:/datasets/alfworld"
Set-Location alfworld
python run_tip_agent.py
```

运行时当前目录必须为 alfworld，因为配置和提示词使用相对路径。ALFWORLD_DATA 应包含 json_2.1.1、logic 子目录。脚本名使用 run_tip_agent.py，避免与第三方 alfworld 包重名。

## 3. WebShop

先安装官方 WebShop、其完整依赖、商品数据和索引，再安装本脚本直接依赖：

```bash
python -m pip install openai openpyxl numpy
```

Bash：

```bash
export WSPRO_SEED=233
export WEBSHOP_NUM_EPISODES=500
export WEBSHOP_MAX_STEPS=50
export PYTHONPATH="/path/to/WebShop:${PYTHONPATH:-}"
cd /path/to/WebShop
python /path/to/发布/webshop/run_tip_agent.py
```

PowerShell：

```powershell
$env:WSPRO_SEED = "233"
$env:WEBSHOP_NUM_EPISODES = "500"
$env:WEBSHOP_MAX_STEPS = "50"
$env:PYTHONPATH = "D:/path/to/WebShop;$env:PYTHONPATH"
Set-Location "D:/path/to/WebShop"
python "C:/Users/zzh15/Desktop/发布/webshop/run_tip_agent.py"
```

使用 WebShop 根目录作为工作目录，便于上游环境读取资源；输出也写到当前工作目录。本脚本是本地环境版本，不需要单独启动 HTTP 服务。更换路径时同步修改上述路径。

## 4. InterCode-SQL

安装：

```bash
python -m pip install requests scipy tiktoken
```

从“发布”根目录检查命令行参数：

```bash
python intercode_sql/run_tip_agent.py --help
```

Linux/WSL 单任务运行：

```bash
python intercode_sql/run_tip_agent.py --data-path /path/to/tasks.json --sqlite-root /path/to/databases --task-index 0 --limit 1 --max-turns 10 --candidates-k 5 --log-path sql_smoke.jsonl --work-root sql_work
```

PowerShell 单任务运行：

```powershell
python intercode_sql/run_tip_agent.py --data-path "D:/datasets/sql/tasks.json" --sqlite-root "D:/datasets/sql/databases" --task-index 0 --limit 1 --max-turns 10 --candidates-k 5 --log-path sql_smoke.jsonl --work-root sql_work
```

完整评测将 --limit 改为实际评测任务数，并确认从 --task-index 开始不越界。多任务入口支持 --best-of，默认 1；取最优不是独立重复实验取均值。任务 JSON、数据库不随本包提供。随包 sqlite_sql 文件夹须与 SQL 入口脚本保持当前相对位置。

## 5. 保存结果

各次运行使用不同工作目录或及时另存结果，避免覆盖。日志、结果表和 API 配置不是本次代码发布文件的一部分。再次运行前核对模型 ID、接口、参数、依赖与数据版本。
