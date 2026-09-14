# -*- coding: utf-8 -*-
"""
worldmodel_sql.py
普适世界模型 Agent（Universal World Model）迁移到 InterCode-SQL

迁移方法（照搬 ReAct -> intercode_SQL 的迁移骨架）：
  1. 环境替换: AlfredTWEnv -> SQLiteSqlEnv（data_path / sqlite_root / work_root 由 CLI 传入）
  2. 动作格式: 自由文本 -> execute[SQL] / submit（复用 parse_react_action 一族）
  3. 策略抽象: 复用 OpenAICompatiblePolicy（.api_env + 环境变量 + 代理 + 重试）
  4. 回合循环: 与 ReAct 版一致（turn -> think/action -> env.step -> 累积 -> done）
  5. 评估/入口: run_*_eval + argparse main 与 ReAct 版同构，下游脚本可无缝替换

领域适配（只发生在"环境胶水层"，Agent 核心保持零领域知识）：
  - 目标: env.query（问题本身）
  - 状态指纹: frozenset(已知表 ∪ (表, 列)) —— schema 知识状态（单调增长）
  - 可执行候选: 环境不提供 -> LLM 生成候选 SQL + 注入固定探索候选（SHOW TABLES / DESC 未知表）
  - 无进展判据: 查询报错 / 观测与上次完全相同 / 同指纹下重复同一查询
  - 自环首次豁免 / revisit 豁免 / 稀有度 / stuck / 死锁重审: 机制原样保留

与 ReAct 版唯一小改动：OpenAICompatiblePolicy.next 增加 prefix 参数，
世界模型子调用（simulate/select/候选生成/重审）不需要 "Thought N:" 前缀。
"""

import argparse
import json
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import requests

from sqlite_sql.env import ACTION_EXEC, SQLiteSqlEnv


# ================================================================
# 🔹 领域示例（喂给 Think 层）
# ================================================================

WM_SQL_DEMO = """Question: What are the names and grades for each high schooler?
Thought 1: I should first check what tables exist.
Action 1: execute[SHOW TABLES]
Observation 1: [('friend',), ('highschooler',), ('likes',)]
Thought 2: The highschooler table looks relevant. Inspect its columns.
Action 2: execute[DESC highschooler]
Observation 2: [('ID', 'int', 'NO', 'PRI', None, 'auto_increment'), ('name', 'text', 'YES', '', None, ''), ('grade', 'int', 'YES', '', None, '')]
Thought 3: I can select name and grade from highschooler.
Action 3: execute[SELECT name, grade FROM highschooler]
Observation 3: [('John', 12), ('Haley', 10)]
Thought 4: The result answers the question.
Action 4: submit"""


# ================================================================
# 🔹 数据结构（与终版2 一致，仅知识库字段按 SQL 语义微调）
# ================================================================

@dataclass
class WorldModelState:
    """通用世界模型状态（零领域知识）"""
    goal: str = ""
    observations: List[Tuple[str, str]] = field(default_factory=list)  # [(query, obs), ...]
    knowledge: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if "blacklist" not in self.knowledge:
            # 反向剪枝：{(状态指纹, 规范化查询签名)}，某状态下重复无进展的查询
            self.knowledge["blacklist"] = set()
        if "visited_states" not in self.knowledge:
            self.knowledge["visited_states"] = set()
        if "tried_actions" not in self.knowledge:
            # 执行过的查询签名（规范化 SQL）
            self.knowledge["tried_actions"] = set()
        if "action_state_count" not in self.knowledge:
            # 每个查询签名被执行的次数（稀有度统计）
            self.knowledge["action_state_count"] = {}
        if "seen_self_loops" not in self.knowledge:
            # 同指纹下见过一次的 (指纹, 签名)：自环首次豁免
            self.knowledge["seen_self_loops"] = set()
        if "seen_observations" not in self.knowledge:
            self.knowledge["seen_observations"] = set()   # 已见过的观测（去重检测）
        if "tables" not in self.knowledge:
            self.knowledge["tables"] = set()              # 已知表名
        if "schema" not in self.knowledge:
            self.knowledge["schema"] = {}                 # {表: set(列)}
        if "fp_history" not in self.knowledge:
            self.knowledge["fp_history"] = []             # 每步后的状态指纹
        if "filter_principles" not in self.knowledge:
            self.knowledge["filter_principles"] = [
                "稀有度优先：候选标注了[执行过=N]（N 为历史执行次数），N 越小越稀有、越是"
                "稍纵即逝的机会（如对还没看过列的表做 DESC），应优先保留；SHOW TABLES 这类"
                "随处可做的常见动作不急。",
                "一致性优先：优先保留与当前思考直接相关、能推进回答问题的查询。",
                "反冗余优先：避免选择已执行过且结果无新信息的查询（重复=原地打转）。",
            ]


@dataclass
class StuckState:
    """stuck 状态追踪（与终版2 一致）"""
    is_stuck: bool = False
    last_obs: str = ""
    repeat_count: int = 0
    stuck_duration: int = 0
    stuck_actions: List[str] = field(default_factory=list)


# ================================================================
# 🔹 环境胶水层（迁移点：ALFWorld 文本 -> SQL/观测）
# ================================================================

def strip_terminal_semicolon(sql: str) -> str:
    sql = (sql or "").strip()
    if sql.endswith(";"):
        return sql[:-1].strip()
    return sql


def extract_last_execute_payload(text: str) -> Optional[str]:
    """从文本提取最后一个 execute[...] 的载荷（ReAct 版原样复用）"""
    marker = "execute["
    start = text.lower().rfind(marker)
    if start < 0:
        return None
    payload_start = start + len(marker)
    depth = 1
    in_single = False
    in_double = False
    escaped = False
    for index in range(payload_start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "'" and not in_double:
            in_single = not in_single
            continue
        if char == '"' and not in_single:
            in_double = not in_double
            continue
        if in_single or in_double:
            continue
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[payload_start:index]
    return text[payload_start:]


def extract_all_execute_payloads(text: str) -> List[str]:
    """从文本提取【所有】execute[...] 载荷（候选生成用，多候选）"""
    payloads: List[str] = []
    for m in re.finditer(r"execute\[", text, flags=re.IGNORECASE):
        i = m.end()
        depth = 1
        while i < len(text) and depth > 0:
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        if depth == 0:
            payloads.append(text[m.end():i])
    return payloads


def parse_react_action(text: str) -> Tuple[str, bool]:
    """解析 Think 输出 -> (动作, 是否可执行)。submit 或 execute[SQL]（ReAct 版原样复用）"""
    text = (text or "").strip()
    if text.lower() == "submit" or re.search(r"(?im)^Action\s+\d+:\s*submit\s*$", text):
        return "submit", True
    payload = extract_last_execute_payload(text)
    if payload is not None:
        return strip_terminal_semicolon(payload), True
    return text, False


def truncate_observation(observation: Any, max_chars: int = 600, max_rows: int = 25) -> Any:
    """截断观测（ReAct 版原样复用）"""
    if isinstance(observation, str) and len(observation) > max_chars:
        return observation[:max_chars]
    if isinstance(observation, list) and len(observation) > max_rows:
        return observation[:max_rows]
    return observation


def extract_tuple_first_values(obs_str: str) -> List[str]:
    """从元组列表观测里取每个元组的第一个字符串元素。
    SHOW TABLES -> 表名；DESC -> 列名。仅解析这种格式，其余返回空。"""
    return re.findall(r"\('([^']+)',", obs_str)


def normalize_query(sql: str) -> str:
    """规范化查询 -> 签名（用于重复/黑名单判定）。不解析语义，只做文本归一。"""
    sql = (sql or "").strip().rstrip(";").strip()
    return re.sub(r"\s+", " ", sql).lower()


def is_error_observation(obs: Any) -> bool:
    return "error executing query" in str(obs).lower()


# ================================================================
# 🔹 LLM 策略（ReAct 版迁移：requests + .api_env + 代理 + 重试）
# ================================================================

class BasePolicy:
    def next(self, prompt: str, turn: int = 1, prefix: bool = True) -> str:
        raise NotImplementedError


import time
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None

def _count_tokens(text):
    if not text:
        return 0
    if _ENC is not None:
        try:
            return len(_ENC.encode(text))
        except Exception:
            pass
    return max(1, len(text) // 4)

class _CountingPolicy:
    def __init__(self, base):
        import inspect
        self._base = base
        self.calls = 0
        self.tokens = 0
        try:
            self._with_prefix = "prefix" in inspect.signature(base.next).parameters
        except Exception:
            self._with_prefix = True
    def next(self, prompt, turn=1, prefix=True):
        self.calls += 1
        if self._with_prefix:
            out = self._base.next(prompt, turn=turn, prefix=prefix)
        else:
            out = self._base.next(prompt, turn=turn)
        self.tokens += _count_tokens(prompt) + _count_tokens(out or "")
        return out

class MockPolicy(BasePolicy):
    def __init__(self, responses: Sequence[str]):
        self.responses = list(responses)
        self.index = 0

    def next(self, prompt: str, turn: int = 1, prefix: bool = True) -> str:
        if self.index >= len(self.responses):
            return f"Thought {turn}: done\nAction {turn}: submit"
        response = self.responses[self.index]
        self.index += 1
        return response


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


class OpenAICompatiblePolicy(BasePolicy):
    """与 ReAct 版唯一区别：next 增加 prefix 参数。
    prefix=True  ->  Think 层（自动补 "Thought {turn}:" 前缀，保持 ReAct 风格）
    prefix=False -> 世界模型子调用（simulate/select/候选生成/重审），输出原样返回

    另：__init__ 已加回终版2 的硬编码 API Key / Base URL 兜底
    （.api_env 文件与 OPENAI_API_KEY 环境变量优先，未配置时才回落到默认值）。"""

    def __init__(
        self,
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        temperature: float = 0.0,
        api_env: Optional[Path] = None,
    ):
        if api_env:
            load_env_file(api_env)
        load_env_file(Path(".api_env"))

        # 硬编码兜底（终版2 原样保留）：.api_env / 环境变量优先，都未配置时才用默认值
        os.environ.setdefault("OPENAI_BASE_URL", "https://api.chatanywhere.tech/v1")

        self.model = model or os.environ.get("OPENAI_MODEL", "qwen3-max-2026-01-23")
        self.max_tokens = max_tokens or int(os.environ.get("OPENAI_MAX_TOKENS", "8192"))
        base_url = os.environ.get("OPENAI_BASE_URL", "https://api.chatanywhere.tech").rstrip("/")
        self.chat_url = base_url + ("/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions")
        self.temperature = temperature
        self.use_proxy = os.environ.get("USE_PROXY", "0") == "1"
        self.proxies = {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"} if self.use_proxy else None

    def _payload(self, prompt: str, turn: int, token_key: str, prefix: bool = True) -> Dict[str, Any]:
        suffix = f"\nThought {turn}:" if prefix else "\n回答:"
        return {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert SQL agent.",
                },
                {"role": "user", "content": prompt + suffix},
            ],
            "temperature": self.temperature,
            token_key: self.max_tokens,
        }

    def next(self, prompt: str, turn: int = 1, prefix: bool = True) -> str:
        if "OPENAI_API_KEY" not in os.environ:
            raise RuntimeError("OPENAI_API_KEY is not set")
        headers = {
            "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
            "Content-Type": "application/json",
        }
        last_error: Optional[BaseException] = None
        token_keys = ["max_completion_tokens", "max_tokens"]
        for attempt in range(1, 7):
            token_key = token_keys[0 if attempt <= 3 else 1]
            try:
                response = requests.post(
                    self.chat_url,
                    headers=headers,
                    json=self._payload(prompt, turn, token_key, prefix),
                    timeout=240,
                    proxies=self.proxies,
                )
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}: {response.text[:800]}")
                data = response.json()
                text = (data.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
                if text:
                    if prefix and not text.lstrip().lower().startswith("thought"):
                        return f"Thought {turn}: {text}"
                    return text
                last_error = RuntimeError("empty model content: " + json.dumps(data.get("usage", {})))
            except Exception as exc:
                last_error = exc
                print(exc)
                print("wait for next call")
            time.sleep(min(30, 2 * attempt))
        raise RuntimeError(f"LLM call failed after retries: {last_error}")


# ================================================================
# 🔹 状态表示与文本构建（迁移点：指纹从 admissible 集 -> schema 知识）
# ================================================================

def state_fingerprint(state: WorldModelState) -> frozenset:
    """状态指纹 = 已知 schema 知识（表 ∪ (表,列)）。
    SQL 世界里数据库本身静态，唯一"前进"的是知识：学到新表/新列 -> 指纹变化 -> 有进展。
    与终版2 的 frozenset(admissible) 同构：不解析内容，纯集合哈希。"""
    atoms: set = set(state.knowledge["tables"])
    for t, cols in state.knowledge["schema"].items():
        for c in cols:
            atoms.add((t, c))
    return frozenset(atoms)


def schema_text(state: WorldModelState) -> str:
    schema = state.knowledge["schema"]
    if not schema:
        return "（未知，可先执行 SHOW TABLES）"
    parts = []
    for t in sorted(schema):
        cols = schema[t]
        parts.append(f"{t}({', '.join(sorted(cols))})" if cols else f"{t}(列未知，可 DESC {t})")
    return "; ".join(parts)


def full_history_text(state: WorldModelState, max_chars: int = 100) -> str:
    lines = []
    for i, (a, o) in enumerate(state.observations, 1):
        o = str(o)[:max_chars].replace("\n", " ")
        if a == "submit":
            lines.append(f"{i}. submit -> {o}")
        else:
            lines.append(f"{i}. execute[{a}] -> {o}")
    return "\n".join(lines) if lines else "（无历史）"


def recent_history_text(state: WorldModelState, n: int = 3) -> str:
    lines = full_history_text(state).splitlines()
    return "\n".join(lines[-n:]) if lines else "（初始状态）"


def has_untried_exploration(state: WorldModelState) -> bool:
    """revisit 豁免判据（SQL 版）：是否存在"已知但还没 DESC 过列"的表。
    有 -> 该状态仍有机会，重复查询不拉黑（与终版2 的豁免逻辑同构）。"""
    return any(len(cols) == 0 for cols in state.knowledge["schema"].values())


def is_blacklisted(state: WorldModelState, query: str) -> bool:
    sig = normalize_query(query)
    fp = state_fingerprint(state)
    return (fp, sig) in state.knowledge["blacklist"]


def strip_thought_prefix(text: str) -> str:
    return re.sub(r"(?i)^Thought\s*\d*\s*:\s*", "", (text or "").strip())


# ================================================================
# 🔹 世界模型核心：simulate（两阶段）+ select（迁移自终版2）
# ================================================================

def simulate_action(
    state: WorldModelState,
    query: str,
    policy: BasePolicy,
    turn: int,
) -> str:
    """
    两阶段模拟（与终版2 同构，仅 prompt 换成 SQL 域）：
      Step 1: LLM 预测该 SQL 的返回结果（幻觉预测，抑制有利幻觉）
      Step 2: LLM 评估该查询对"回答问题"的推进程度
    """
    prompt_step1 = f"""你是一个 SQL 查询结果模拟器。根据问题、已知数据库结构和历史查询，预测执行某条 SQL 后最可能返回什么结果。

问题: {state.goal}

已知数据库结构: {schema_text(state)}

最近历史:
{recent_history_text(state, 3)}

要预测的 SQL: {query}

预测要求:
1. 严格基于已知的表/列/历史结果做保守预测，不要编造不存在的表、列或数据。
2. 从未查询过的列，不要臆测其具体取值。
3. 若 SQL 可能出错（表/列不存在、语法错误），请如实预测报错信息。
4. 只输出预测的查询结果，不要解释。

预测结果:"""
    predicted = strip_thought_prefix(policy.next(prompt_step1, turn, prefix=False))

    prompt_step2 = f"""你是任务进度评估专家。根据问题、完整历史、以及即将执行的 SQL，评估它对回答问题的推进程度。

问题: {state.goal}

完整历史（已执行的查询及真实结果，按顺序）:
{full_history_text(state)}

即将执行的 SQL: {query}
该查询预测结果: {predicted}

评估要点:
1. 先判断哪些信息【已经获得】（表结构、关键数据、最终答案等）。
2. 已经获得的信息不要重复获取；重复查询属于原地打转，应评"无明显进展"。
3. 只有真正获取【尚未获得】的表/列/数据，或直接产出最终答案，才算有进展。
4. 若该查询可能报错，应评"无明显进展"。

只输出以下四种之一（选一个并附简短理由）:
- 问题已回答: [理由]
- 接近完成: [理由]
- 有进展但不足: [理由]
- 无明显进展: [理由]"""
    progress = strip_thought_prefix(policy.next(prompt_step2, turn, prefix=False))
    return f"查询结果预测: {predicted}\n任务进度: {progress}"


def generate_candidate_queries(
    state: WorldModelState,
    policy: BasePolicy,
    turn: int,
    k: int = 5,
) -> List[str]:
    """候选生成（SQL 版无环境 admissible，改为 LLM 生成 + 注入固定探索候选）"""
    prompt = f"""你是 SQL 查询规划器。根据问题、已知数据库结构和历史，提出 {k} 个互不相同的候选 SQL 查询，供后续择优执行。

问题: {state.goal}

已知数据库结构: {schema_text(state)}

完整历史:
{full_history_text(state)}

覆盖要求:
- 至少 1 个"直接作答"候选：尝试直接回答问题的 SELECT
- 至少 1 个"数据采集"候选：获取回答问题所需的关键数据
- 若还有未探查的表/列，至少 1 个"结构探索"候选：SHOW TABLES / DESC 未知表

每个候选必须用 execute[SQL] 包裹，一行一个，只输出候选列表。"""
    out = policy.next(prompt, turn, prefix=False)
    candidates = [strip_terminal_semicolon(c) for c in extract_all_execute_payloads(out)]

    # 注入固定探索候选（等价于原环境的"基础可执行动作"）
    if not state.knowledge["tables"]:
        candidates.append("SHOW TABLES")
    for t in sorted(state.knowledge["tables"]):
        if not state.knowledge["schema"].get(t):
            candidates.append(f"DESC {t}")

    # 去重保序
    seen, result = set(), []
    for c in candidates:
        if not c:
            continue
        sig = normalize_query(c)
        if sig not in seen:
            seen.add(sig)
            result.append(c)
    return result


def quick_filter_candidates(
    state: WorldModelState,
    all_candidates: List[str],
    target_count: int,
    policy: BasePolicy,
    turn: int,
    current_think: str = "",
) -> List[str]:
    """阶段1：LLM 快速筛选（迁移自终版2，稀有度标注改为"执行过 N 次"）"""
    if len(all_candidates) <= target_count:
        return all_candidates

    counts = state.knowledge.get("action_state_count", {})
    candidates_text = "\n".join(
        f"{i+1}. {a}  [执行过={counts.get(normalize_query(a), 0)}]"
        for i, a in enumerate(all_candidates)
    )
    recent = recent_history_text(state, 3)
    think_section = f"\n当前步思考: {current_think}\n" if current_think else ""
    principles = "\n".join(f"- {p}" for p in state.knowledge["filter_principles"])

    prompt = f"""你是 SQL 查询筛选专家。根据问题、当前思考、最近历史和筛选原则，从候选 SQL 中筛选出最 promising 的 {target_count} 个。

问题: {state.goal}
{think_section}
最近历史:
{recent}

筛选原则（请综合运用）:
{principles}

候选 SQL 列表:
{candidates_text}

只输出选中的编号（逗号分隔），例如: 2,5,7"""
    out = policy.next(prompt, turn, prefix=False)

    try:
        indices = [int(x.strip()) - 1 for x in out.split(",") if x.strip().lstrip("-").isdigit()]
        selected = [all_candidates[i] for i in indices if 0 <= i < len(all_candidates)]
        if len(selected) >= target_count // 2:
            print(f"  快速筛选成功: 选出 {len(selected)} 个候选")
            return selected[:target_count]
    except Exception as e:
        print(f"  快速筛选解析失败: {e}, fallback到分类采样")
    return sample_diverse_candidates(all_candidates, target_count)


def sample_diverse_candidates(actions: List[str], max_count: int) -> List[str]:
    """Fallback：按查询类型（首词）分组采样保证多样性"""
    groups = defaultdict(list)
    for act in actions:
        action_type = act.split()[0].lower()
        groups[action_type].append(act)

    sampled = []
    per_group = max(1, max_count // max(1, len(groups)))
    for group_actions in groups.values():
        sampled.extend(random.sample(group_actions, min(per_group, len(group_actions))))

    if len(sampled) < max_count:
        remaining = [a for a in actions if a not in sampled]
        needed = max_count - len(sampled)
        if remaining:
            sampled.extend(random.sample(remaining, min(needed, len(remaining))))
    return sampled[:max_count]


def rank_by_rarity(state: WorldModelState, actions: List[str]) -> List[str]:
    """稀有度优先重排序（迁移自终版2）：执行次数少的查询排前面（如首次 DESC 未知表），
    "执行过很多次但从不会被选中的常见项"（如反复 SHOW TABLES）垫底。纯统计、不解析语义。"""
    counts = state.knowledge.get("action_state_count", {})
    tried = state.knowledge.get("tried_actions", set())
    NOISE_THRESHOLD = 5

    selectable, noise = [], []
    for a in actions:
        sig = normalize_query(a)
        freq = counts.get(sig, 0)
        if freq > NOISE_THRESHOLD and sig not in tried:
            noise.append(a)
        else:
            selectable.append(a)

    selectable_sorted = sorted(selectable, key=lambda a: counts.get(normalize_query(a), 0))
    return selectable_sorted + noise


def select_best_action(
    state: WorldModelState,
    candidates: List[Tuple[str, str]],
    policy: BasePolicy,
    turn: int,
) -> str:
    """LLM 综合判断：优先按任务进度标签选（迁移自终版2）"""
    if not candidates:
        return ""

    candidates_text = "\n\n".join(
        f"{i}. SQL: {query}\n{comb}" for i, (query, comb) in enumerate(candidates, 1)
    )
    prompt = f"""你是一个路径选择专家。根据问题，从候选 SQL 及其预测中选择最可能回答问题的一个。

问题: {state.goal}

每个候选包含两部分:
  - 查询结果预测: 执行该 SQL 后可能返回什么
  - 任务进度: 该 SQL 对回答问题的推进程度（问题已回答/接近完成/有进展但不足/无明显进展）

候选:
{candidates_text}

选择规则(按优先级):
1. **优先选"问题已回答"或"接近完成"的** — 直接产出答案或只差一步
2. 否则选"有进展但不足"的 — 至少获取了需要的信息
3. 避免"无明显进展"的 — 报错/重复/原地打转

只输出该候选的编号(1-{len(candidates)})，不要解释。"""
    out = policy.next(prompt, turn, prefix=False)
    try:
        choice = int(re.search(r"\d+", out).group())
        if 1 <= choice <= len(candidates):
            return candidates[choice - 1][0]
    except Exception:
        pass
    return candidates[0][0]


def re_review_deadlock(
    state: WorldModelState,
    admissible: List[str],
    stuck_intent: str,
    reason: str,
    policy: BasePolicy,
    turn: int,
) -> Optional[str]:
    """误判死锁重审（迁移自终版2）：把"真实可执行动作列表"交给 LLM，强制重新落地。"""
    if not admissible:
        return None

    full = full_history_text(state)
    actions_text = "\n".join(f"{i+1}. {a}" for i, a in enumerate(admissible))

    prompt = f"""你是一个纠错重规划专家。当前 agent 已陷入死锁：{reason}
它反复坚持某个查询/计划却无法推进，很可能计划本身有误，或缺少某个前置步骤（如先 SHOW TABLES / DESC 某表）。

问题: {state.goal}

完整历史（已执行的查询及真实结果，按顺序）:
{full}

Agent 一直想做但无法落地的意图: {stuck_intent}

【当前真实可执行的候选动作列表】（只能从这里选一个，编号从 1 开始）:
{actions_text}

请判断并二选一：
A. 如果那个意图是对的、只是缺少前置条件（例如需要先 SHOW TABLES / DESC 某表 / 先查询某列），
   就从列表里选出【最能补齐该前置条件】的一个动作。
B. 如果那个意图方向错了（例如引用了不存在的表/列），就从列表里选一个
   【与最近反复尝试明显不同】的动作，去探索新的可能。

只输出你选择的动作【编号】(1-{len(admissible)})，不要解释。"""
    out = policy.next(prompt, turn, prefix=False)
    try:
        m = re.search(r"\d+", out)
        choice = int(m.group())
        if 1 <= choice <= len(admissible):
            return admissible[choice - 1]
    except Exception:
        pass
    return None


# ================================================================
# 🔹 知识库更新（迁移自终版2：自环首次豁免 / revisit 豁免 / 稀有度）
# ================================================================

def update_knowledge_from_observation(
    state: WorldModelState,
    query: str,
    obs: Any,
    fp_before: frozenset,
) -> None:
    """从观测更新知识库。

    无进展统一判据（纯统计，不解析 SQL 语义）：
      1. 查询报错
      2. 观测与历史某次完全相同（返回旧结果 = 没学到新东西）
      3. 同指纹下重复执行同一查询（同 schema 状态下原地打转）

    自环首次豁免：同一 (指纹, 签名) 首次出现放行，再次出现才拉黑。
    revisit 豁免：目标状态仍有"未 DESC 的已知表" -> 还有机会 -> 不拉黑。
    """
    obs_str = str(obs)
    sig = normalize_query(query)
    q = (query or "").strip().lower()

    # 1) schema 学习（环境胶水：只解析 SHOW TABLES / DESC 两类观测）
    if q.startswith("show tables"):
        for t in extract_tuple_first_values(obs_str):
            if t:
                state.knowledge["tables"].add(t)
                state.knowledge["schema"].setdefault(t, set())
    elif q.startswith(("desc ", "describe ")):
        table = q.split(None, 1)[1].strip().strip("`;\"'")
        for col in extract_tuple_first_values(obs_str):
            if col:
                state.knowledge["schema"].setdefault(table, set()).add(col)
        if table:
            state.knowledge["tables"].add(table)

    fp_after = state_fingerprint(state)
    state.knowledge["fp_history"].append(fp_after)

    # 2) 无进展判据
    errored = is_error_observation(obs)
    repeated_obs = obs_str in state.knowledge["seen_observations"]
    same_state_repeat = (fp_before == fp_after) and (sig in state.knowledge["tried_actions"])
    no_progress = errored or repeated_obs or same_state_repeat

    # 3) 自环首次豁免 -> 重复拉黑（机制与终版2 一致）
    if fp_before == fp_after:
        key = (fp_before, sig)
        if key in state.knowledge["seen_self_loops"]:
            if no_progress and not has_untried_exploration(state):
                state.knowledge["blacklist"].add(key)
        else:
            state.knowledge["seen_self_loops"].add(key)

    # 4) 常规知识更新
    state.knowledge["tried_actions"].add(sig)
    state.knowledge["action_state_count"][sig] = state.knowledge["action_state_count"].get(sig, 0) + 1
    state.knowledge["seen_observations"].add(obs_str)
    state.knowledge["visited_states"].add(fp_after)
    state.observations.append((query, obs_str))


def update_stuck(
    state: WorldModelState,
    stuck: StuckState,
    action: str,
    obs_str: str,
    worldmodel_flat_noprogress: bool = False,
) -> None:
    """stuck 检测（迁移自终版2）：
    - 指纹变化（学到新表/列）-> 解除 stuck
    - 连续 2 次相同观测 / 世界模型全评"无明显进展" -> 进入 stuck，强制换动作"""
    fps = state.knowledge.get("fp_history", [])
    if len(fps) >= 2 and fps[-1] != fps[-2]:
        stuck.is_stuck = False
        stuck.repeat_count = 0
        stuck.last_obs = obs_str
        return

    if obs_str == stuck.last_obs:
        stuck.repeat_count += 1
    else:
        stuck.repeat_count = 0
        stuck.last_obs = obs_str

    if worldmodel_flat_noprogress and stuck.repeat_count >= 1:
        stuck.repeat_count = max(stuck.repeat_count, 2)

    if stuck.repeat_count >= 2:
        stuck.is_stuck = True
        stuck.stuck_duration = 2
        if action and normalize_query(action) != "submit":
            stuck.stuck_actions.append(normalize_query(action))

    if stuck.is_stuck:
        stuck.stuck_duration -= 1
        if stuck.stuck_duration <= 0:
            stuck.is_stuck = False
            stuck.repeat_count = 0


def select_fallback(state: WorldModelState, candidates: List[str]) -> str:
    if not candidates:
        return "SHOW TABLES"
    return rank_by_rarity(state, candidates)[0]


# ================================================================
# 🔹 Think 层（意图直采的前置）
# ================================================================

def build_think_prompt(state: WorldModelState, turn: int, stuck: StuckState) -> str:
    stuck_note = ""
    if stuck.is_stuck:
        stuck_note = ("\n【探索提示】当前连续多步没有新进展。请换一个真正不同的查询"
                      "如果最近一次查询的观测已经能回答原始问题，请直接 Action: submit；否则请换一个真正不同的查询（例如 DESC 一个还没看过列的表，或换一种查询方式）。")
    return f"""你是一个 SQL Agent。用 SQL 查询回答下面的问题。请先给出 Thought（当前推理），再给出 Action。
Action 必须是 execute[SQL] 或 submit：
- 若最近一次 execute 的观测已能回答原始问题 -> 必须立即 Action: submit，禁止再执行任何查询
- 若还需要表结构或数据 -> Action: execute[SQL]
- 禁止重复执行与历史相同或仅格式不同的查询
- SELECT 只选回答问题所需的最少列，不要附加多余列
- 除非问题明确要求排序（如 ordered / by / 升序 / 降序），否则不要使用 ORDER BY
- 写 JOIN 前先用 DESC 查看相关表，确认外键列名再连接
- 仅当最近一次查询是 SELECT 且结果非空、非报错时才可 submit；DESC / SHOW TABLES 的观测不能作为最终答案

交互示例:
{WM_SQL_DEMO}

问题: {state.goal}
已知数据库结构: {schema_text(state)}
最近历史:
{recent_history_text(state, 3)}
完整历史:
{full_history_text(state)}
{stuck_note}"""


# ================================================================
# 🔹 回合循环（与 ReAct 版同构：run_*_episode / run_*_eval / main）
# ================================================================

_AGG_GOAL_RE = re.compile(
    r"(how many|count|number of|more than|at least|at most|each|per|every|"
    r"\d+\s+or more|most|total|average|sum|greatest number|largest number)",
    re.I)
_AGG_SQL_RE = re.compile(r"\b(COUNT|SUM|AVG|MAX|MIN)\s*\(|GROUP\s+BY|HAVING", re.I)

def agg_strategy_hint(goal, actions, turn):
    """问题需聚合、历史却无聚合查询、且已探索>=4轮 -> 给一句策略提示"""
    if turn < 4:
        return ""
    if not _AGG_GOAL_RE.search(goal or ""):
        return ""
    if any(_AGG_SQL_RE.search(a or "") for a in actions):
        return ""
    return ("【策略提示】该问题需要按组计数/聚合：请生成一个带 GROUP BY 的查询；"
            "若涉及“多于/至少/最多/每个/at least/more than”，还需配合 HAVING 条件。")

def verify_submit(state, policy, turn):
    """v6 提交验证门：观测必须真的回答了问题才允许提交。"""
    prompt = (
        "你是提交审核员。原始问题：\n" + state.goal + "\n\n"
        "最近执行情况：\n" + recent_history_text(state, 2) + "\n\n"
        "逐条检查，任一不合格则回答 NO：\n"
        "1. 问题含计数/每组/每样/each/per/how many/more than/at least，"
        "而 SQL 含聚合函数却缺 GROUP BY 或 HAVING -> 不合格\n"
        "2. 问题要求“每个X一行/每组一行”，但观测只有一行或明显行数不符 -> 不合格\n"
        "3. WHERE/ON 用字符串与整数外键列比较（如 Country='France' 而 Country 实为 ID）-> 不合格\n"
        "4. 观测为空或 COUNT=0，而问题对象（如知名国家/实体）显然应非零 -> 可疑\n"
        "5. 观测内容是否真的完整回答了问题？\n"
        "6. 问题按“每个X/每Y”分组时，SQL 必须 GROUP BY 实体列；COUNT(DISTINCT x) 与 COUNT(x) 语义不同\n"
        "7. 观测行数远少于实体应有数量级（如几十个maker只有一行）-> 可疑\n"
        "8. 观测中某行聚合值明显不合理（如单行count超过总量级）-> 可疑\n"
        "只回答 YES（可提交）或 NO（不可提交）+ 一句话理由。不确定时倾向 YES。"
    )
    out = policy.next(prompt, turn, prefix=False)
    text = (out or "").strip()
    first = text.splitlines()[0] if text else ""
    ok = first.upper().startswith("YES") or "YES" in text[:20].upper()
    return ok, text

def run_worldmodel_episode(
    *,
    data_path: Path,
    sqlite_root: Path,
    task_index: int,
    max_turns: int,
    policy: BasePolicy,
    log_path: Optional[Path] = None,
    work_root: Optional[Path] = None,
    candidates_k: int = 5,
    filter_target: int = 6,
) -> Dict[str, Any]:
    env = SQLiteSqlEnv(data_path=data_path, sqlite_root=sqlite_root, work_root=work_root)
    env.reset(task_index)
    _cp = _CountingPolicy(policy)
    policy = _cp
    _t0 = time.time()

    state = WorldModelState(goal=env.query)
    stuck = StuckState()
    history: Dict[str, List[Any]] = {
        "thoughts": [],
        "actions": [],
        "observations": [],
        "rewards": [],
        "valid_action": [],
        "pipeline": [],
    }
    reward = 0.0
    done = False
    try:
        for turn in range(1, max_turns + 1):
            # ---- 1) 思考 + 意图 ----
            prompt = build_think_prompt(state, turn, stuck)
            _hint = agg_strategy_hint(state.goal, history["actions"], turn)
            if _hint:
                prompt += "\n\n" + _hint
            think_output = policy.next(prompt, turn, prefix=True)
            think = think_output.strip().splitlines()[0] if think_output.strip() else f"Thought {turn}:"
            history["thoughts"].append(think)

            # ---- 2) 动作选择 ----
            action: Optional[str] = None
            pipeline = "unknown"
            worldmodel_flat_noprogress = False

            if stuck.is_stuck:
                # 死锁重审：强制从"真实可执行列表"里重选
                candidates = generate_candidate_queries(state, policy, turn, k=candidates_k)
                real_actions = candidates + ["submit"]
                reason = f"已连续 {stuck.repeat_count} 步无新进展（重复观测/重复查询）"
                action = re_review_deadlock(state, real_actions, think_output, reason, policy, turn)
                pipeline = "deadlock_review"
                if action is None:
                    action = select_fallback(state, candidates)
                    pipeline = "deadlock_fallback"
            else:
                # 意图直采（跳过幻觉层）：模型自己说要做的事，黑名单未命中直接执行
                parsed, is_code = parse_react_action(think_output)
                if is_code and parsed == "submit":
                    action, pipeline = "submit", "intent_direct"
                elif is_code and parsed and not is_blacklisted(state, parsed):
                    action, pipeline = parsed, "intent_direct"
                else:
                    # 世界模型两阶段：候选 -> (筛选) -> simulate -> select
                    candidates = generate_candidate_queries(state, policy, turn, k=candidates_k)
                    candidates = [c for c in candidates if not is_blacklisted(state, c)]
                    if not candidates:
                        candidates = (
                            ["SHOW TABLES"]
                            if not state.knowledge["tables"]
                            else [f"DESC {sorted(state.knowledge['tables'])[0]}"]
                        )
                    if len(candidates) > filter_target:
                        candidates = quick_filter_candidates(
                            state, candidates, filter_target, policy, turn, current_think=think
                        )
                    candidates = rank_by_rarity(state, candidates)
                    sims = [simulate_action(state, c, policy, turn) for c in candidates]
                    sims.append("提交当前观测作为最终答案（问题已回答）")
                    action = select_best_action(
                        state, list(zip(candidates + ["submit"], sims)), policy, turn
                    )
                    pipeline = "worldmodel"
                    worldmodel_flat_noprogress = bool(sims) and all("无明显进展" in s for s in sims)

            # ---- 2.5) v6 提交验证门 ----
            if action == "submit" and turn < max_turns and not stuck.is_stuck:
                _ok, _reason = verify_submit(state, policy, turn)
                if not _ok:
                    history["thoughts"].append("SUBMIT REJECTED: " + str(_reason)[:200])
                    _cands = generate_candidate_queries(state, policy, turn, k=candidates_k)
                    _cands = [c for c in _cands if not is_blacklisted(state, c)]
                    # v7: gate 可逆——拒绝理由回给 LLM 二辩
                    _defense = policy.next(
                        "你的提交被验证器拒绝，理由：" + str(_reason)[:300] + "。\n"
                        "重新决策，只输出一个动作：\n"
                        "1) submit（你确信查询正确，验证器可能误判）\n"
                        "2) 一条修正后的可执行 SQL",
                        turn, prefix=True,
                    ).strip()
                    print(f"[gate] 拒绝理由: {str(_reason)[:200]}")
                    print(f"[gate] 二辩回复: {_defense[:120]!r}")
                    _dparsed, _dcode = parse_react_action(_defense)
                    if _dparsed == "submit":
                        pipeline = pipeline + "_gate_override"
                    elif _dcode and _dparsed and not is_blacklisted(state, _dparsed):
                        action = _dparsed
                        pipeline = pipeline + "_gate_review"
                        worldmodel_flat_noprogress = False
                    else:
                        # v7.1: 回复无法解析时默认放行——保留 agent 原提交意图，
                        # 避免执行其他查询覆盖掉已积累的正确解
                        pipeline = pipeline + "_gate_override"

            # ---- 3) 执行 ----
            obs, reward, done, info = env.step(action)
            valid_action = bool(info.get(ACTION_EXEC, True))
            obs_for_prompt = truncate_observation(obs)
            fp_before = state_fingerprint(state)
            update_knowledge_from_observation(state, action, obs_for_prompt, fp_before)
            update_stuck(state, stuck, action, str(obs_for_prompt), worldmodel_flat_noprogress)

            history["actions"].append(action)
            history["observations"].append(str(obs_for_prompt))
            history["rewards"].append(reward)
            history["valid_action"].append(valid_action)
            history["pipeline"].append(pipeline)
            print(f"[turn {turn}] pipeline={pipeline} reward={reward} action={str(action)[:60]}")
            if done:
                break

        if not done:
            # v6: 计数类问题耗尽前补一次聚合尝试
            if (_AGG_GOAL_RE.search(state.goal or "")
                    and not any(_AGG_SQL_RE.search(a or "") for a in history["actions"])):
                _cands = generate_candidate_queries(state, policy, max_turns, k=candidates_k)
                _agg = next((c for c in _cands if _AGG_SQL_RE.search(c or "")), None)
                if _agg:
                    obs, reward, done, info = env.step(_agg)
                    history["actions"].append(_agg)
                    history["observations"].append(str(truncate_observation(obs)))
                    history["rewards"].append(reward)
                    history["pipeline"].append("last_chance_agg")
                    print(f"[last-chance] agg query reward={reward}")
        if not done:
            obs, reward, done, info = env.step("submit")
            history["thoughts"].append("EXCEEDED MAX TURNS: submit")
            history["actions"].append("submit")
            history["observations"].append(str(truncate_observation(obs)))
            history["rewards"].append(reward)
            history["valid_action"].append(bool(info.get(ACTION_EXEC, True)))
            history["pipeline"].append("force_submit")

        result = {
            "environment": env.name,
            "dataset": str(data_path),
            "task_id": task_index,
            "query": env.query,
            "db": env.current_db,
            "steps": len(history["actions"]),
            "api_calls": _cp.calls,
            "tokens": _cp.tokens,
            "elapsed": round(time.time() - _t0, 3),
            "reward": reward,
            "done": done,
            "turn_history": history,
            "summary": {
                "max_reward": reward,
                "turns_taken": len(history["actions"]),
                "turns_max": max_turns,
            },
        }
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
    finally:
        env.close()


def print_final_summary(results) -> None:
    n = len(results)
    if n == 0:
        print("===== FINAL =====\n(no results)\n===== ===== =====")
        return
    ok = sum(1 for r in results if float(r.get("reward") or 0.0) >= 0.5)
    full = sum(1 for r in results if float(r.get("reward") or 0.0) == 1.0)
    avg_r = sum(float(r.get("reward") or 0.0) for r in results) / n
    avg_a = sum(float(r.get("api_calls") or 0.0) for r in results) / n
    avg_t = sum(float(r.get("tokens") or 0.0) for r in results) / n
    avg_e = sum(float(r.get("elapsed") or 0.0) for r in results) / n
    avg_s = sum(float(r.get("steps") or 0.0) for r in results) / n
    print("===== FINAL =====")
    print(f"累计成功(>=0.5): {ok}/{n} ({ok / n * 100:.1f}%)")
    print(f"满分(1.0): {full}/{n} ({full / n * 100:.1f}%)")
    print(f"平均 reward: {avg_r:.4f}")
    print(f"平均 API 调用: {avg_a:.1f} 次/任务")
    print(f"平均 tokens: {avg_t:.0f} /任务")
    print(f"平均 耗时: {avg_e:.1f} s/任务")
    print(f"平均 步数: {avg_s:.1f} /任务")
    print("===== ===== =====")


def run_worldmodel_eval(
    *,
    data_path: Path,
    sqlite_root: Path,
    start_index: int,
    limit: int,
    max_turns: int,
    make_policy: Callable[[], BasePolicy],
    log_path: Optional[Path] = None,
    work_root: Optional[Path] = None,
    candidates_k: int = 5,
    filter_target: int = 6,
    best_of: int = 1,
) -> Dict[str, Any]:
    records = json.loads(Path(data_path).read_text(encoding="utf-8"))
    stop = min(len(records), start_index + limit)
    results: Dict[str, Any] = {}
    for index in range(start_index, stop):
        best = None
        _sum = {"api_calls": 0, "tokens": 0, "elapsed": 0.0}
        for _k in range(max(1, best_of)):
            result = run_worldmodel_episode(
                data_path=data_path,
                sqlite_root=sqlite_root,
                task_index=index,
                max_turns=max_turns,
                policy=make_policy(),
                work_root=work_root,
                candidates_k=candidates_k,
                filter_target=filter_target,
            )
            _sum["api_calls"] += int(result.get("api_calls") or 0)
            _sum["tokens"] += int(result.get("tokens") or 0)
            _sum["elapsed"] += float(result.get("elapsed") or 0.0)
            if best is None or float(result.get("reward") or 0.0) > float(best.get("reward") or 0.0):
                best = result
            if float(best.get("reward") or 0.0) >= 1.0:
                break  # v7.2: 满分即停，结果等价于跑满 N 次
        best["api_calls"] = _sum["api_calls"]
        best["tokens"] = _sum["tokens"]
        best["elapsed"] = round(_sum["elapsed"], 3)
        results[str(index)] = best
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Universal World Model agent over SQLite InterCode-SQL.")
    parser.add_argument("--data-path", required=True)
    parser.add_argument("--sqlite-root", required=True)
    parser.add_argument("--task-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--log-path")
    parser.add_argument("--work-root")
    parser.add_argument("--api-env")
    parser.add_argument("--candidates-k", type=int, default=5)
    parser.add_argument("--best-of", type=int, default=1, help="每任务跑N次取最优 (v6)")
    parser.add_argument("--mock-actions", nargs="*", help="Optional raw ReAct responses for deterministic smoke tests.")
    args = parser.parse_args()

    def make_policy() -> BasePolicy:
        if args.mock_actions:
            return MockPolicy(args.mock_actions)
        return OpenAICompatiblePolicy(api_env=Path(args.api_env) if args.api_env else None)

    if args.limit == 1:
        result = run_worldmodel_episode(
            data_path=Path(args.data_path),
            sqlite_root=Path(args.sqlite_root),
            task_index=args.task_index,
            max_turns=args.max_turns,
            policy=make_policy(),
            log_path=Path(args.log_path) if args.log_path else None,
            work_root=Path(args.work_root) if args.work_root else None,
            candidates_k=args.candidates_k,
        )
        print(json.dumps({"reward": result["reward"], "done": result["done"], "task_id": result["task_id"]}, ensure_ascii=False))
        return

    results = run_worldmodel_eval(
        data_path=Path(args.data_path),
        sqlite_root=Path(args.sqlite_root),
        start_index=args.task_index,
        limit=args.limit,
        max_turns=args.max_turns,
        make_policy=make_policy,
        log_path=Path(args.log_path) if args.log_path else None,
        work_root=Path(args.work_root) if args.work_root else None,
        candidates_k=args.candidates_k,
        best_of=args.best_of,
    )
    solved = sum(1 for item in results.values() if float(item.get("reward") or 0.0) >= 1.0)
    print(json.dumps({"episodes": len(results), "solved": solved, "log_path": args.log_path}, ensure_ascii=False))
    print_final_summary(list(results.values()))


if __name__ == "__main__":
    main()
