# 依赖包列表

这是源码直接使用的依赖列表，不是原实验环境的锁定版本，也不包含离线安装包。分别使用各环境兼容的 Python 虚拟环境；下面列出的是顶层包，第三方环境的完整依赖按其自身安装说明安装。

## ALFWorld

| 包或组件 | 用途 |
|---|---|
| alfworld | 文本交互环境，包括 AlfredTWEnv |
| openai（提供 OpenAI 类的版本，如 1.x 或以上） | OpenAI 兼容模型调用 |
| openpyxl | Excel 结果输出 |
| PyYAML | 读取 base_config.yaml |

另需 ALFWorld 游戏数据和逻辑文件。安装来源：[ALFWorld 官方仓库](https://github.com/alfworld/alfworld)。

## WebShop

| 包或组件 | 用途 |
|---|---|
| 官方 WebShop 环境及其依赖 | 提供 web_agent_site.envs.WebAgentTextEnv |
| openai（提供 OpenAI 类的版本） | OpenAI 兼容模型调用 |
| openpyxl | Excel 结果与诊断输出 |
| numpy | 设置环境随机种子 |

商品数据、搜索索引及环境要求的系统组件按 [WebShop 官方仓库](https://github.com/princeton-nlp/WebShop) 安装。不能仅安装 openai 等几个包就认为环境已经完整。

## InterCode-SQL

| 包或组件 | 用途 |
|---|---|
| requests | 模型 API HTTP 调用 |
| scipy | reward.py 中的 Kendall 排序相关性评分 |
| tiktoken | token 计数；缺少时源码使用近似估计 |
| sqlite_sql（本包已包含） | SQLite 环境、探查语句转换及奖励函数 |
| sqlite3（Python 标准库） | 数据库执行，无须通过 pip 安装 |

SciPy 缺失时源码会跳过排序相关性项，可能改变得分；比较实验应保持评分依赖及版本一致。tiktoken 缺失也会改变 token 计数口径。原实验具体版本需以实验机器为准。

sqlite_sql 是本包的本地模块，不是需要从 pip 下载的同名包。该实现是 SQLite 适配器，不等同于直接调用官方 InterCode 环境；基准背景见 [InterCode 官方仓库](https://github.com/princeton-nlp/intercode)。
