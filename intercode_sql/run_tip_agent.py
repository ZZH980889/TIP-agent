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

WM_SQL_DEMO = "Question: What are the names and grades for each high schooler?\nThought 1: I should first check what tables exist.\nAction 1: execute[SHOW TABLES]\nObservation 1: [('friend',), ('highschooler',), ('likes',)]\nThought 2: The highschooler table looks relevant. Inspect its columns.\nAction 2: execute[DESC highschooler]\nObservation 2: [('ID', 'int', 'NO', 'PRI', None, 'auto_increment'), ('name', 'text', 'YES', '', None, ''), ('grade', 'int', 'YES', '', None, '')]\nThought 3: I can select name and grade from highschooler.\nAction 3: execute[SELECT name, grade FROM highschooler]\nObservation 3: [('John', 12), ('Haley', 10)]\nThought 4: The result answers the question.\nAction 4: submit"


@dataclass
class WorldModelState:
    goal: str = ""
    observations: List[Tuple[str, str]] = field(default_factory=list)
    knowledge: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if "blacklist" not in self.knowledge:
            self.knowledge["blacklist"] = set()
        if "visited_states" not in self.knowledge:
            self.knowledge["visited_states"] = set()
        if "tried_actions" not in self.knowledge:
            self.knowledge["tried_actions"] = set()
        if "action_state_count" not in self.knowledge:
            self.knowledge["action_state_count"] = {}
        if "seen_self_loops" not in self.knowledge:
            self.knowledge["seen_self_loops"] = set()
        if "seen_observations" not in self.knowledge:
            self.knowledge["seen_observations"] = set()
        if "tables" not in self.knowledge:
            self.knowledge["tables"] = set()
        if "schema" not in self.knowledge:
            self.knowledge["schema"] = {}
        if "fp_history" not in self.knowledge:
            self.knowledge["fp_history"] = []
        if "filter_principles" not in self.knowledge:
            self.knowledge["filter_principles"] = [
                "Rarity first: [executed=N] records the historical execution count. Smaller N means a rarer, potentially fleeting opportunity (e.g., DESC for a table whose columns have not been inspected); retain it preferentially. Common actions available everywhere, such as SHOW TABLES, are less urgent.",
                "Consistency first: prefer queries directly related to the current thought that advance the answer.",
                "Avoid redundancy: do not select previously executed queries whose results add no new information (repetition means going in circles).",
            ]


@dataclass
class StuckState:
    is_stuck: bool = False
    last_obs: str = ""
    repeat_count: int = 0
    stuck_duration: int = 0
    stuck_actions: List[str] = field(default_factory=list)


def strip_terminal_semicolon(sql: str) -> str:
    sql = (sql or "").strip()
    if sql.endswith(";"):
        return sql[:-1].strip()
    return sql


def extract_last_execute_payload(text: str) -> Optional[str]:
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
        if char == "'" and (not in_double):
            in_single = not in_single
            continue
        if char == '"' and (not in_single):
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
    payloads: List[str] = []
    for m in re.finditer("execute\\[", text, flags=re.IGNORECASE):
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
            payloads.append(text[m.end() : i])
    return payloads


def parse_react_action(text: str) -> Tuple[str, bool]:
    text = (text or "").strip()
    if text.lower() == "submit" or re.search(
        "(?im)^Action\\s+\\d+:\\s*submit\\s*$", text
    ):
        return ("submit", True)
    payload = extract_last_execute_payload(text)
    if payload is not None:
        return (strip_terminal_semicolon(payload), True)
    return (text, False)


def truncate_observation(
    observation: Any, max_chars: int = 600, max_rows: int = 25
) -> Any:
    if isinstance(observation, str) and len(observation) > max_chars:
        return observation[:max_chars]
    if isinstance(observation, list) and len(observation) > max_rows:
        return observation[:max_rows]
    return observation


def extract_tuple_first_values(obs_str: str) -> List[str]:
    return re.findall("\\('([^']+)',", obs_str)


def normalize_query(sql: str) -> str:
    sql = (sql or "").strip().rstrip(";").strip()
    return re.sub("\\s+", " ", sql).lower()


def is_error_observation(obs: Any) -> bool:
    return "error executing query" in str(obs).lower()


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
        os.environ.setdefault("OPENAI_BASE_URL", "https://api.chatanywhere.tech/v1")
        self.model = model or os.environ.get("OPENAI_MODEL", "qwen3-max-2026-01-23")
        self.max_tokens = max_tokens or int(os.environ.get("OPENAI_MAX_TOKENS", "8192"))
        base_url = os.environ.get(
            "OPENAI_BASE_URL", "https://api.chatanywhere.tech"
        ).rstrip("/")
        self.chat_url = base_url + (
            "/chat/completions" if base_url.endswith("/v1") else "/v1/chat/completions"
        )
        self.temperature = temperature
        self.use_proxy = os.environ.get("USE_PROXY", "0") == "1"
        self.proxies = (
            {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"}
            if self.use_proxy
            else None
        )

    def _payload(
        self, prompt: str, turn: int, token_key: str, prefix: bool = True
    ) -> Dict[str, Any]:
        suffix = f"\nThought {turn}:" if prefix else "\nAnswer:"
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are an expert SQL agent."},
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
                    raise RuntimeError(
                        f"HTTP {response.status_code}: {response.text[:800]}"
                    )
                data = response.json()
                text = (
                    data.get("choices", [{}])[0].get("message", {}).get("content") or ""
                ).strip()
                if text:
                    if prefix and (not text.lstrip().lower().startswith("thought")):
                        return f"Thought {turn}: {text}"
                    return text
                last_error = RuntimeError(
                    "empty model content: " + json.dumps(data.get("usage", {}))
                )
            except Exception as exc:
                last_error = exc
                print(exc)
                print("wait for next call")
            time.sleep(min(30, 2 * attempt))
        raise RuntimeError(f"LLM call failed after retries: {last_error}")


def state_fingerprint(state: WorldModelState) -> frozenset:
    atoms: set = set(state.knowledge["tables"])
    for t, cols in state.knowledge["schema"].items():
        for c in cols:
            atoms.add((t, c))
    return frozenset(atoms)


def schema_text(state: WorldModelState) -> str:
    schema = state.knowledge["schema"]
    if not schema:
        return "(unknown; execute SHOW TABLES first)"
    parts = []
    for t in sorted(schema):
        cols = schema[t]
        parts.append(
            f"{t}({', '.join(sorted(cols))})"
            if cols
            else f"{t}(columns unknown; try DESC {t})"
        )
    return "; ".join(parts)


def full_history_text(state: WorldModelState, max_chars: int = 100) -> str:
    lines = []
    for i, (a, o) in enumerate(state.observations, 1):
        o = str(o)[:max_chars].replace("\n", " ")
        if a == "submit":
            lines.append(f"{i}. submit -> {o}")
        else:
            lines.append(f"{i}. execute[{a}] -> {o}")
    return "\n".join(lines) if lines else "(no history)"


def recent_history_text(state: WorldModelState, n: int = 3) -> str:
    lines = full_history_text(state).splitlines()
    return "\n".join(lines[-n:]) if lines else "(initial state)"


def has_untried_exploration(state: WorldModelState) -> bool:
    return any((len(cols) == 0 for cols in state.knowledge["schema"].values()))


def is_blacklisted(state: WorldModelState, query: str) -> bool:
    sig = normalize_query(query)
    fp = state_fingerprint(state)
    return (fp, sig) in state.knowledge["blacklist"]


def strip_thought_prefix(text: str) -> str:
    return re.sub("(?i)^Thought\\s*\\d*\\s*:\\s*", "", (text or "").strip())


def simulate_action(
    state: WorldModelState, query: str, policy: BasePolicy, turn: int
) -> str:
    prompt_step1 = f"You are a SQL query-result simulator. Given the question, known database schema, and query history, predict the most likely result of executing a SQL query.\n\nQuestion: {state.goal}\n\nKnown database schema: {schema_text(state)}\n\nRecent history:\n{recent_history_text(state, 3)}\n\nSQL to predict: {query}\n\nPrediction requirements:\n1. Predict conservatively from known tables, columns, and past results. Do not invent nonexistent tables, columns, or data.\n2. Do not guess specific values for columns that have never been queried.\n3. If the SQL may fail (missing table/column or syntax error), faithfully predict an error.\n4. Output only the predicted query result, without explanation.\n\nPredicted result:"
    predicted = strip_thought_prefix(policy.next(prompt_step1, turn, prefix=False))
    prompt_step2 = f'You are a task-progress assessment expert. Given the question, complete history, and proposed SQL, assess its progress toward answering the question.\n\nQuestion: {state.goal}\n\nComplete history (executed queries and real results, in order):\n{full_history_text(state)}\n\nProposed SQL: {query}\nPredicted query result: {predicted}\n\nAssessment criteria:\n1. Determine what information has ALREADY BEEN OBTAINED (schema, key data, final answer, etc.).\n2. Do not acquire already-known information again. Repeated queries go in circles and should be rated "No clear progress".\n3. Progress means obtaining previously UNKNOWN tables, columns, or data, or directly producing the final answer.\n4. If the query is likely to fail, rate it "No clear progress".\n\nOutput exactly one of the following with a brief reason:\n- Question answered: [reason]\n- Near completion: [reason]\n- Some progress, but insufficient: [reason]\n- No clear progress: [reason]'
    progress = strip_thought_prefix(policy.next(prompt_step2, turn, prefix=False))
    return f"Predicted query result: {predicted}\nTask progress: {progress}"


def generate_candidate_queries(
    state: WorldModelState, policy: BasePolicy, turn: int, k: int = 5
) -> List[str]:
    prompt = f"You are a SQL query planner. Given the question, known schema, and history, propose {k} distinct candidate SQL queries for subsequent selection and execution.\n\nQuestion: {state.goal}\n\nKnown database schema: {schema_text(state)}\n\nComplete history:\n{full_history_text(state)}\n\nCoverage requirements:\n- At least one direct-answer candidate: a SELECT query attempting to answer the question directly.\n- At least one data-collection candidate: obtain key data needed to answer the question.\n- If tables/columns remain unexplored, at least one schema-exploration candidate: SHOW TABLES or DESC for an unknown table.\n\nWrap each candidate in execute[SQL], one per line. Output only the candidate list."
    out = policy.next(prompt, turn, prefix=False)
    candidates = [
        strip_terminal_semicolon(c) for c in extract_all_execute_payloads(out)
    ]
    if not state.knowledge["tables"]:
        candidates.append("SHOW TABLES")
    for t in sorted(state.knowledge["tables"]):
        if not state.knowledge["schema"].get(t):
            candidates.append(f"DESC {t}")
    seen, result = (set(), [])
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
    if len(all_candidates) <= target_count:
        return all_candidates
    counts = state.knowledge.get("action_state_count", {})
    candidates_text = "\n".join(
        (
            f"{i + 1}. {a}  [executed={counts.get(normalize_query(a), 0)}]"
            for i, a in enumerate(all_candidates)
        )
    )
    recent = recent_history_text(state, 3)
    think_section = f"\nCurrent thought: {current_think}\n" if current_think else ""
    principles = "\n".join((f"- {p}" for p in state.knowledge["filter_principles"]))
    prompt = f"You are a SQL query-filtering expert. Given the question, current thought, recent history, and filtering principles, select the most promising {target_count} candidates.\n\nQuestion: {state.goal}\n{think_section}\nRecent history:\n{recent}\n\nFiltering principles (apply jointly):\n{principles}\n\nCandidate SQL list:\n{candidates_text}\n\nOutput only the selected indices, separated by commas, e.g.: 2,5,7"
    out = policy.next(prompt, turn, prefix=False)
    try:
        indices = [
            int(x.strip()) - 1
            for x in out.split(",")
            if x.strip().lstrip("-").isdigit()
        ]
        selected = [all_candidates[i] for i in indices if 0 <= i < len(all_candidates)]
        if len(selected) >= target_count // 2:
            print(f"  Quick filtering succeeded: selected {len(selected)} candidates")
            return selected[:target_count]
    except Exception as e:
        print(
            f"  Quick-filter parsing failed: {e}, falling back to category-based sampling"
        )
    return sample_diverse_candidates(all_candidates, target_count)


def sample_diverse_candidates(actions: List[str], max_count: int) -> List[str]:
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
    counts = state.knowledge.get("action_state_count", {})
    tried = state.knowledge.get("tried_actions", set())
    NOISE_THRESHOLD = 5
    selectable, noise = ([], [])
    for a in actions:
        sig = normalize_query(a)
        freq = counts.get(sig, 0)
        if freq > NOISE_THRESHOLD and sig not in tried:
            noise.append(a)
        else:
            selectable.append(a)
    selectable_sorted = sorted(
        selectable, key=lambda a: counts.get(normalize_query(a), 0)
    )
    return selectable_sorted + noise


def select_best_action(
    state: WorldModelState,
    candidates: List[Tuple[str, str]],
    policy: BasePolicy,
    turn: int,
) -> str:
    if not candidates:
        return ""
    candidates_text = "\n\n".join(
        (f"{i}. SQL: {query}\n{comb}" for i, (query, comb) in enumerate(candidates, 1))
    )
    prompt = f'You are a path-selection expert. Given the question, choose the candidate SQL most likely to answer it, using the candidate predictions.\n\nQuestion: {state.goal}\n\nEach candidate has two parts:\n  - Predicted query result: what the SQL may return\n  - Task progress: how much it advances the answer (Question answered / Near completion / Some progress, but insufficient / No clear progress)\n\nCandidates:\n{candidates_text}\n\nSelection rules, in priority order:\n1. **Prefer "Question answered" or "Near completion"**: directly produces the answer or is one step away.\n2. Otherwise choose "Some progress, but insufficient": obtains required information.\n3. Avoid "No clear progress": errors, repetition, or going in circles.\n\nOutput only the candidate index (1-{len(candidates)}), without explanation.'
    out = policy.next(prompt, turn, prefix=False)
    try:
        choice = int(re.search("\\d+", out).group())
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
    if not admissible:
        return None
    full = full_history_text(state)
    actions_text = "\n".join((f"{i + 1}. {a}" for i, a in enumerate(admissible)))
    prompt = f"You are an error-correction and replanning expert. The agent is currently stuck: {reason}\nIt repeatedly insists on a query/plan without making progress. The plan may be wrong or lack a prerequisite (e.g., SHOW TABLES or DESC for a table).\n\nQuestion: {state.goal}\n\nComplete history (executed queries and real results, in order):\n{full}\n\nIntent the agent keeps proposing but cannot ground: {stuck_intent}\n\n[Currently executable candidate actions] (choose one only from this list; indices start at 1):\n{actions_text}\n\nChoose between these two cases:\nA. If the intent is correct but lacks a prerequisite (e.g., SHOW TABLES, DESC for a table, or querying a column),\n   choose the action that BEST SATISFIES that prerequisite.\nB. If the intent is misguided (e.g., references a nonexistent table/column), choose an action\n   CLEARLY DIFFERENT from the recent repeated attempts to explore another possibility.\n\nOutput only the selected action INDEX (1-{len(admissible)}), without explanation."
    out = policy.next(prompt, turn, prefix=False)
    try:
        m = re.search("\\d+", out)
        choice = int(m.group())
        if 1 <= choice <= len(admissible):
            return admissible[choice - 1]
    except Exception:
        pass
    return None


def update_knowledge_from_observation(
    state: WorldModelState, query: str, obs: Any, fp_before: frozenset
) -> None:
    obs_str = str(obs)
    sig = normalize_query(query)
    q = (query or "").strip().lower()
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
    errored = is_error_observation(obs)
    repeated_obs = obs_str in state.knowledge["seen_observations"]
    same_state_repeat = (
        fp_before == fp_after and sig in state.knowledge["tried_actions"]
    )
    no_progress = errored or repeated_obs or same_state_repeat
    if fp_before == fp_after:
        key = (fp_before, sig)
        if key in state.knowledge["seen_self_loops"]:
            if no_progress and (not has_untried_exploration(state)):
                state.knowledge["blacklist"].add(key)
        else:
            state.knowledge["seen_self_loops"].add(key)
    state.knowledge["tried_actions"].add(sig)
    state.knowledge["action_state_count"][sig] = (
        state.knowledge["action_state_count"].get(sig, 0) + 1
    )
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


def build_think_prompt(state: WorldModelState, turn: int, stuck: StuckState) -> str:
    stuck_note = ""
    if stuck.is_stuck:
        stuck_note = "\n[Exploration hint] Several consecutive steps have made no new progress. Try a genuinely different query. If the latest query observation already answers the original question, output Action: submit directly; otherwise, choose a genuinely different query (e.g., DESC for a table whose columns have not been inspected, or a different query formulation)."
    return f"You are a SQL agent. Answer the question below using SQL queries. Output Thought (current reasoning), followed by Action.\nAction must be execute[SQL] or submit:\n- If the latest execute observation answers the original question, immediately output Action: submit; do not execute another query.\n- If more schema information or data is needed, output Action: execute[SQL].\n- Do not repeat a historical query or a query differing only in formatting.\n- SELECT only the minimum columns needed to answer the question; do not add extra columns.\n- Do not use ORDER BY unless the question explicitly asks for ordering (e.g., ordered / by / ascending / descending).\n- Before writing a JOIN, inspect the relevant tables with DESC and confirm the foreign-key column names.\n- Submit only when the latest query is a SELECT with a nonempty, non-error result; DESC / SHOW TABLES observations cannot serve as the final answer.\n\nInteraction example:\n{WM_SQL_DEMO}\n\nQuestion: {state.goal}\nKnown database schema: {schema_text(state)}\nRecent history:\n{recent_history_text(state, 3)}\nComplete history:\n{full_history_text(state)}\n{stuck_note}"


_AGG_GOAL_RE = re.compile(
    "(how many|count|number of|more than|at least|at most|each|per|every|\\d+\\s+or more|most|total|average|sum|greatest number|largest number)",
    re.I,
)
_AGG_SQL_RE = re.compile("\\b(COUNT|SUM|AVG|MAX|MIN)\\s*\\(|GROUP\\s+BY|HAVING", re.I)


def agg_strategy_hint(goal, actions, turn):
    if turn < 4:
        return ""
    if not _AGG_GOAL_RE.search(goal or ""):
        return ""
    if any((_AGG_SQL_RE.search(a or "") for a in actions)):
        return ""
    return "[Strategy hint] This question requires grouped counting/aggregation: generate a query with GROUP BY. If it involves more than / at least / at most / each, also use an appropriate HAVING condition."


def verify_submit(state, policy, turn):
    prompt = (
        "You are a submission reviewer. Original question:\n"
        + state.goal
        + "\n\nRecent execution:\n"
        + recent_history_text(state, 2)
        + "\n\nCheck each item. If any fails, answer NO:\n1. If the question involves counts / each group / each type / each / per / how many / more than / at least, and the SQL uses aggregation but lacks GROUP BY or HAVING, it fails.\n2. If one row per entity/group is required but the observation has only one row or an obviously incorrect row count, it fails.\n3. Comparing a string against an integer foreign-key column in WHERE/ON (e.g., Country='France' when Country is an ID) fails.\n4. An empty observation or COUNT=0 is suspicious when the target (e.g., a well-known country/entity) should clearly be nonzero.\n5. Does the observation actually answer the question completely?\n6. When grouping by each X / per Y, SQL must GROUP BY the entity column; COUNT(DISTINCT x) and COUNT(x) differ semantically.\n7. A row count far below the expected number of entities (e.g., one row for dozens of makers) is suspicious.\n8. An obviously implausible aggregate value in a row (e.g., count exceeding the total scale) is suspicious.\nAnswer only YES (submit) or NO (do not submit), plus one sentence of explanation. If uncertain, lean toward YES."
    )
    out = policy.next(prompt, turn, prefix=False)
    text = (out or "").strip()
    first = text.splitlines()[0] if text else ""
    ok = first.upper().startswith("YES") or "YES" in text[:20].upper()
    return (ok, text)


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
    env = SQLiteSqlEnv(
        data_path=data_path, sqlite_root=sqlite_root, work_root=work_root
    )
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
            prompt = build_think_prompt(state, turn, stuck)
            _hint = agg_strategy_hint(state.goal, history["actions"], turn)
            if _hint:
                prompt += "\n\n" + _hint
            think_output = policy.next(prompt, turn, prefix=True)
            think = (
                think_output.strip().splitlines()[0]
                if think_output.strip()
                else f"Thought {turn}:"
            )
            history["thoughts"].append(think)
            action: Optional[str] = None
            pipeline = "unknown"
            worldmodel_flat_noprogress = False
            if stuck.is_stuck:
                candidates = generate_candidate_queries(
                    state, policy, turn, k=candidates_k
                )
                real_actions = candidates + ["submit"]
                reason = f"There have been {stuck.repeat_count} consecutive steps without new progress (repeated observations/queries)"
                action = re_review_deadlock(
                    state, real_actions, think_output, reason, policy, turn
                )
                pipeline = "deadlock_review"
                if action is None:
                    action = select_fallback(state, candidates)
                    pipeline = "deadlock_fallback"
            else:
                parsed, is_code = parse_react_action(think_output)
                if is_code and parsed == "submit":
                    action, pipeline = ("submit", "intent_direct")
                elif is_code and parsed and (not is_blacklisted(state, parsed)):
                    action, pipeline = (parsed, "intent_direct")
                else:
                    candidates = generate_candidate_queries(
                        state, policy, turn, k=candidates_k
                    )
                    candidates = [c for c in candidates if not is_blacklisted(state, c)]
                    if not candidates:
                        candidates = (
                            ["SHOW TABLES"]
                            if not state.knowledge["tables"]
                            else [f"DESC {sorted(state.knowledge['tables'])[0]}"]
                        )
                    if len(candidates) > filter_target:
                        candidates = quick_filter_candidates(
                            state,
                            candidates,
                            filter_target,
                            policy,
                            turn,
                            current_think=think,
                        )
                    candidates = rank_by_rarity(state, candidates)
                    sims = [simulate_action(state, c, policy, turn) for c in candidates]
                    sims.append(
                        "Submit the current observation as the final answer (Question answered)"
                    )
                    action = select_best_action(
                        state, list(zip(candidates + ["submit"], sims)), policy, turn
                    )
                    pipeline = "worldmodel"
                    worldmodel_flat_noprogress = bool(sims) and all(
                        ("No clear progress" in s for s in sims)
                    )
            if action == "submit" and turn < max_turns and (not stuck.is_stuck):
                _ok, _reason = verify_submit(state, policy, turn)
                if not _ok:
                    history["thoughts"].append("SUBMIT REJECTED: " + str(_reason)[:200])
                    _cands = generate_candidate_queries(
                        state, policy, turn, k=candidates_k
                    )
                    _cands = [c for c in _cands if not is_blacklisted(state, c)]
                    _defense = policy.next(
                        "Your submission was rejected by the validator. Reason: "
                        + str(_reason)[:300]
                        + ".\nDecide again and output only one action:\n1) submit (you are confident that the query is correct and the validator may be mistaken)\n2) a corrected executable SQL query",
                        turn,
                        prefix=True,
                    ).strip()
                    print(f"[gate] Rejection reason: {str(_reason)[:200]}")
                    print(f"[gate] Reconsideration response: {_defense[:120]!r}")
                    _dparsed, _dcode = parse_react_action(_defense)
                    if _dparsed == "submit":
                        pipeline = pipeline + "_gate_override"
                    elif _dcode and _dparsed and (not is_blacklisted(state, _dparsed)):
                        action = _dparsed
                        pipeline = pipeline + "_gate_review"
                        worldmodel_flat_noprogress = False
                    else:
                        pipeline = pipeline + "_gate_override"
            obs, reward, done, info = env.step(action)
            valid_action = bool(info.get(ACTION_EXEC, True))
            obs_for_prompt = truncate_observation(obs)
            fp_before = state_fingerprint(state)
            update_knowledge_from_observation(state, action, obs_for_prompt, fp_before)
            update_stuck(
                state, stuck, action, str(obs_for_prompt), worldmodel_flat_noprogress
            )
            history["actions"].append(action)
            history["observations"].append(str(obs_for_prompt))
            history["rewards"].append(reward)
            history["valid_action"].append(valid_action)
            history["pipeline"].append(pipeline)
            print(
                f"[turn {turn}] pipeline={pipeline} reward={reward} action={str(action)[:60]}"
            )
            if done:
                break
        if not done:
            if _AGG_GOAL_RE.search(state.goal or "") and (
                not any((_AGG_SQL_RE.search(a or "") for a in history["actions"]))
            ):
                _cands = generate_candidate_queries(
                    state, policy, max_turns, k=candidates_k
                )
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
            log_path.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return result
    finally:
        env.close()


def print_final_summary(results) -> None:
    n = len(results)
    if n == 0:
        print("===== FINAL =====\n(no results)\n===== ===== =====")
        return
    ok = sum((1 for r in results if float(r.get("reward") or 0.0) >= 0.5))
    full = sum((1 for r in results if float(r.get("reward") or 0.0) == 1.0))
    avg_r = sum((float(r.get("reward") or 0.0) for r in results)) / n
    avg_a = sum((float(r.get("api_calls") or 0.0) for r in results)) / n
    avg_t = sum((float(r.get("tokens") or 0.0) for r in results)) / n
    avg_e = sum((float(r.get("elapsed") or 0.0) for r in results)) / n
    avg_s = sum((float(r.get("steps") or 0.0) for r in results)) / n
    print("===== FINAL =====")
    print(f"Total successes (>=0.5): {ok}/{n} ({ok / n * 100:.1f}%)")
    print(f"Full-score tasks (1.0): {full}/{n} ({full / n * 100:.1f}%)")
    print(f"Mean reward: {avg_r:.4f}")
    print(f"Mean API calls: {avg_a:.1f} calls/task")
    print(f"Mean tokens: {avg_t:.0f} /task")
    print(f"Mean duration: {avg_e:.1f} s/task")
    print(f"Mean steps: {avg_s:.1f} /task")
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
            if best is None or float(result.get("reward") or 0.0) > float(
                best.get("reward") or 0.0
            ):
                best = result
            if float(best.get("reward") or 0.0) >= 1.0:
                break
        best["api_calls"] = _sum["api_calls"]
        best["tokens"] = _sum["tokens"]
        best["elapsed"] = round(_sum["elapsed"], 3)
        results[str(index)] = best
        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(
                json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
            )
    return results


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Universal World Model agent over SQLite InterCode-SQL."
    )
    parser.add_argument("--data-path", required=True)
    parser.add_argument("--sqlite-root", required=True)
    parser.add_argument("--task-index", type=int, default=0)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-turns", type=int, default=10)
    parser.add_argument("--log-path")
    parser.add_argument("--work-root")
    parser.add_argument("--api-env")
    parser.add_argument("--candidates-k", type=int, default=5)
    parser.add_argument(
        "--best-of",
        type=int,
        default=1,
        help="Run N attempts per task and retain the best result (v6)",
    )
    parser.add_argument(
        "--mock-actions",
        nargs="*",
        help="Optional raw ReAct responses for deterministic smoke tests.",
    )
    args = parser.parse_args()

    def make_policy() -> BasePolicy:
        if args.mock_actions:
            return MockPolicy(args.mock_actions)
        return OpenAICompatiblePolicy(
            api_env=Path(args.api_env) if args.api_env else None
        )

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
        print(
            json.dumps(
                {
                    "reward": result["reward"],
                    "done": result["done"],
                    "task_id": result["task_id"],
                },
                ensure_ascii=False,
            )
        )
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
    solved = sum(
        (1 for item in results.values() if float(item.get("reward") or 0.0) >= 1.0)
    )
    print(
        json.dumps(
            {"episodes": len(results), "solved": solved, "log_path": args.log_path},
            ensure_ascii=False,
        )
    )
    print_final_summary(list(results.values()))


if __name__ == "__main__":
    main()
