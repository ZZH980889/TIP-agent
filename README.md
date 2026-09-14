# TIP-Agent 实验代码

本目录提供 TIP-Agent 在 ALFWorld、WebShop 和 InterCode-SQL 三个环境上的代码、依赖列表及执行语句。不包含论文、实验结果、数据集、模型密钥或虚拟环境。

## 文件结构

```text
发布/
├── README.md                 # 本使用说明
├── DEPENDENCIES.md           # 三个环境的依赖列表
├── RUN_COMMANDS.md           # API 配置、安装和执行语句
├── alfworld/
│   ├── run_tip_agent.py
│   ├── base_config.yaml
│   └── prompts/alfworld_3prompts.json
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

SQL 子目录中的四个环境文件是智能体实际依赖的本地模块，必须一起上传。ALFWorld 的配置和提示词同样不能省略。未包含与 TIP-Agent 执行无关的其他算法脚本或备份文件。

## 使用顺序

1. 参照 [DEPENDENCIES.md](DEPENDENCIES.md)，准备对应环境及依赖。
2. 按 [RUN_COMMANDS.md](RUN_COMMANDS.md) 设置自己的 API 密钥、接口和模型名称。
3. 准备环境数据，替换命令中的示例路径，再运行对应入口。
4. 保存每次运行的独立输出，避免后续运行覆盖。

ALFWorld 从独立 JSON 中加载 few-shot 提示。WebShop 使用本地 WebAgentTextEnv。SQL 使用随包提供的 SQLite 适配器，不需要把无关的 SQL 基线脚本一起运行。

## 数据准备

- ALFWorld：自行准备官方数据，ALFWORLD_DATA 指向包含 json_2.1.1 和 logic 的根目录。
- WebShop：自行准备官方环境、商品数据和搜索索引；确保 Python 能导入 web_agent_site。
- SQL：提供任务 JSON 数组，每条记录包含 query（问题）、db（数据库名）和 gold（参考 SQL，用于评分）。例如字段结构为 `{"query": "...", "db": "database_name", "gold": "SELECT ..."}`，不是附带的真实测试任务。数据库可按 `数据库根目录/database_name/database_name.sqlite` 放置，也支持根目录下同名 .sqlite/.db 文件。

## 输出与参数

- ALFWorld：当前入口评测 134 个 OOD 任务，默认每任务最多 50 步；在工作目录生成 test4_wm_universal_results-1step.xlsx。
- WebShop：默认 WSPRO_SEED=233、500 个会话、每任务最多 50 步；默认生成 webshop_pro_results_V4DIAG_PRO_500.xlsx。会话数等参数见执行说明。
- SQL：通过命令行指定任务范围、每任务轮数、候选数量和日志位置；--limit 1 可先检查单任务。
- API 请求重试、SQL 的 --best-of 与独立重复实验是不同操作。代码未自动汇总五次独立运行。

## 注意事项

发布副本已移除硬编码 API 密钥；不要把真实密钥或 .api_env 上传到 GitHub。SQL 默认关闭原机器本地代理，必要时显式设置 USE_PROXY。算法逻辑未因本次整理而改变。

依赖列表依据现有代码整理，未锁定原实验版本。本目录不承诺与论文所有运行版本完全一致；完整环境运行和结果复现尚未验证。源码中的历史开发注释不是本包生成的新实验结果。使用第三方环境、数据和提示词时，应遵守其原有许可；本次整理未代作者授予新的开源许可证。
