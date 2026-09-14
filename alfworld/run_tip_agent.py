"""
test4.py
普适世界模型 Agent（Universal World Model）— 预测+评估两阶段版本

核心设计：
1. 世界模型两阶段：
   - Step1: LLM 预测执行动作后的观测结果（幻觉预测）
   - Step2: LLM 评估该动作对任务目标的推进程度（已完成/接近完成/有进展/无进展）
   - Select: 优先根据任务进度评估选择动作，而不是只看预测观测的"丰富度"
2. 意图直采：模型自己说要做的动作，优先直接执行（跳过幻觉层）
3. 运行时知识库：筛选原则（含稀有度优先）指导候选筛选
4. 反向剪枝：识别无进展动作（自环首次豁免 + revisit 豁免）
5. Stuck 检测：动作循环时强制多样化探索

普适性：
- 代码零领域知识，换环境不改代码
- 状态指纹 = 可执行动作集合（不解析动作内容）
- 知识库全是环境无关的通用原则（稀有度、一致性、反冗余）
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple, Any
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment
import yaml
import alfworld
from alfworld.agents.environment.alfred_tw_env import AlfredTWEnv
from openai import OpenAI


# ================================================================
# 🔹 数据结构
# ================================================================

@dataclass
class StepRecord:
    """每一步的记录（think / action / observation）"""
    kind: str
    text: str


@dataclass
class WorldModelState:
    """通用世界模型状态（零领域知识）"""
    goal: str = ""                                      # 任务目标
    observations: List[Tuple[str, str]] = field(default_factory=list)  # [(action, obs), ...]
    knowledge: Dict[str, Any] = field(default_factory=dict)  # 运行时知识库
    # knowledge 结构示例：
    # {
    #   "blacklist": {(状态指纹, 动作), ...},   # 反向剪枝：某状态下无进展的动作组合
    #   "visited_states": {状态指纹, ...},       # 访问过的状态
    #   "tried_actions": {动作, ...},            # 执行过的动作
    #   "action_state_count": {动作: 出现次数},  # 稀有度统计
    #   "filter_principles": [原则字符串, ...],  # 候选筛选原则库（含稀有度原则）
    # }

    def __post_init__(self):
        """初始化知识库结构"""
        if "blacklist" not in self.knowledge:
            # 反向剪枝：记录 (状态指纹, 动作) 的无进展组合。
            # 状态指纹 = frozenset(admissible)，即环境提供的可执行动作集合，
            # 代码不解析其内容，纯当作状态哈希 —— 零领域知识。
            self.knowledge["blacklist"] = set()
        if "visited_states" not in self.knowledge:
            # 访问过的状态指纹集合，用于识别"回到来过的状态"（后退/原地踏步）。
            self.knowledge["visited_states"] = set()
        if "tried_actions" not in self.knowledge:
            # 历史上真正执行过的动作集合（不解析内容）。
            # 用途1：revisit 豁免——若目标状态仍有"没试过的动作"，则回去不算后退。
            # 用途2：稀有动作加权的辅助。
            self.knowledge["tried_actions"] = set()
        if "action_state_count" not in self.knowledge:
            # 每个动作在"历史访问过的 admissible 集合"里出现过多少次。
            # 出现越少 → 越稀有 → 越像"稍纵即逝的机会"（如只在某处可做的交互）。
            # 纯频率统计，不解析动作文本 —— 完全环境无关。
            self.knowledge["action_state_count"] = {}
        if "seen_self_loops" not in self.knowledge:
            # 见过一次的 (状态指纹, 动作) 自环组合。用于"自环首次豁免"：
            # 首次自环放行（可能改变了不可见属性，如 cool/heat），重复自环才拉黑。
            self.knowledge["seen_self_loops"] = set()
        if "filter_principles" not in self.knowledge:
            # 候选筛选原则库（供兜底的 quick_filter 参考）。
            # 全部是环境无关的通用启发式，不含任何领域词（take/open/fridge...）。
            # 可扩展：以后学到的新原则也追加到这里，让筛选越来越聪明。
            self.knowledge["filter_principles"] = [
                # 稀有度原则（第一条）：稀有动作 = 只在少数状态下才出现的机会，
                # 一旦离开当前状态可能就再也做不了，应优先保留；随处可选的动作不急。
                "稀有度优先：候选后标注了[稀有度=N]，N 越小表示这个动作越少见、越可能是"
                "稍纵即逝的机会，应优先保留；N 很大的动作随处可做，不急于选。",
                # 一致性原则：尊重模型自己的推理链
                "优先保留与当前思考直接相关、能推进任务目标的动作。",
                # 反冗余原则：不解析语义，纯经验
                "避免选择明显不改变处境的动作（如只是查看、原地确认之类）。",
            ]


@dataclass
class StuckState:
    """stuck 状态追踪"""
    is_stuck: bool = False
    last_obs: str = ""
    repeat_count: int = 0
    stuck_duration: int = 0  # stuck 状态持续影响的剩余步数
    stuck_actions: List[str] = field(default_factory=list)  # stuck 期间禁用的动作模式


# ================================================================
# 🔹 LLM 调用
# ================================================================

def build_client() -> OpenAI:
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.chatanywhere.tech/v1")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required")
    return OpenAI(base_url=base_url, api_key=api_key)


def llm(prompt: str, client: OpenAI, model: str, max_tokens: int) -> str:
    """LLM 调用（带重试）"""
    for attempt in range(2):
        try:
            messages = [{"role": "user", "content": prompt}]
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                stream=False,
                max_tokens=max_tokens,
                timeout=60.0,  # 添加60秒超时
            )
            content = response.choices[0].message.content or ""
            return content.strip()
        except Exception as e:
            print(f"LLM调用失败 (attempt {attempt+1}/2): {str(e)[:100]}")
            if attempt == 0:
                time.sleep(2)
                continue
            return ""


# ================================================================
# 🔹 通用工具
# ================================================================

def process_ob(ob: str) -> str:
    """处理观测文本"""
    if ob.startswith("You arrive at loc "):
        ob = ob[ob.find(". ") + 2:]
    return ob


def get_admissible(info: Optional[Dict]) -> List[str]:
    """从环境获取可执行动作列表（标准接口，普适）

    普适性修正：不再人为剔除 examine/look 等观察类动作。
    理由（见补丁分析）：
    1. 硬编码 'examine'/'look' 违背"零领域知识、换环境不改代码"的设计，
       换语言/换动词的环境会失效或误伤。
    2. 观察动作不改变可执行集 → 属于自环，已有的"自环首次豁免→重复即拉黑"
       机制会自动放行一次、重复时拉黑，无需在入口硬删。
    3. 保留观察能力对"信息获取型意图"（如 look in bowl 查内容）是必要的，
       也是死锁重审能选到有效动作的前提。
    """
    if isinstance(info, dict):
        ac = info.get("admissible_commands")
        if isinstance(ac, list) and len(ac) > 0 and isinstance(ac[0], list):
            return [a for a in ac[0] if isinstance(a, str)]
    return []


def extract_goal(ob: str) -> str:
    """从初始观测里提取任务目标"""
    marker = "Your task is to:"
    if marker in ob:
        return ob.split(marker, 1)[1].strip().split("\n")[0]
    return ""


# ================================================================
# 🔹 世界模型核心：simulate + select
# ================================================================

def simulate_action(
    state: WorldModelState,
    action: str,
    client: OpenAI,
    model: str,
    max_tokens: int,
) -> str:
    """
    世界模型核心(两阶段)：LLM 预测 + LLM 评估任务进度

    Step 1: 预测执行该动作后最可能看到的观测结果(幻觉预测)
    Step 2: 基于 step1 的预测观测,评估该动作对任务目标的推进程度

    输入：当前状态（目标、历史观测）+ 候选动作
    输出：step1 观测 + step2 任务进度评估(拼接成一段文本供 select 使用)
    """
    # 最近 3 步历史（供 step1 预测观测用，只需近景）
    recent_obs = state.observations[-3:] if len(state.observations) > 3 else state.observations
    history_text = "\n".join([f"{i+1}. 执行: {a}\n   观测: {o}" for i, (a, o) in enumerate(recent_obs)])
    if not history_text:
        history_text = "（初始状态，无历史）"

    # 完整历史（供 step2 评估用）：任务进度评估必须看到"已经做过什么"，
    # 否则会反复把"已经完成的子步骤"（如已冷却）误判为仍需执行，从而奖励回头路。
    # 这里给出全部已执行动作及其真实观测（压缩单行），完全环境无关、不解析内容。
    if state.observations:
        full_history_text = "\n".join(
            [f"{i+1}. 执行: {a} → 观测: {o[:80]}" for i, (a, o) in enumerate(state.observations)]
        )
    else:
        full_history_text = "（初始状态，无历史）"

    # Step 1: 预测观测
    # 约束：明确要求"移动/查看类动作不得凭空生成任务目标物"，抑制"go to X → 看到 book/tomato"
    # 这类有利幻觉——它会误导 step2 评高分、进而奖励瞎逛（见死锁分析）。
    prompt_step1 = f"""你是一个世界模型模拟器。根据任务目标和历史观测，预测执行某个动作后最可能看到的结果。

任务目标: {state.goal}

最近历史:
{history_text}

现在要预测的动作: {action}

预测要求（重要）:
1. 严格基于历史观测和该动作的字面效果做保守预测，不要为了"推进任务"而编造有利信息。
2. 若某个位置在历史中已被访问且未见到目标物，再次前往【不应】凭空出现目标物。
3. 单纯的移动（go to）或打开（open）动作，通常只改变位置或可见容器内容，
   不要臆测里面恰好有任务所需的关键物品，除非历史已明确显示。

请预测执行该动作后，最可能看到什么观测结果。只输出预测的观测文本，不要解释。
"""

    predicted_obs = llm(prompt_step1, client=client, model=model, max_tokens=max_tokens)
    predicted_obs = predicted_obs.strip()

    # Step 2: 评估任务进度(关键——不再幻想下一步,而是评估当前动作的任务推进度)
    # 关键修正：把"完整已执行历史"喂给评估器。只看单个动作会让评估器丧失记忆，
    # 反复认为"还需冷却/还需拿取"这类已完成的子步骤，从而把回头路评为"有进展"。
    prompt_step2 = f"""你是一个任务进度评估专家。根据任务目标、到目前为止已完成的完整历史，以及即将执行的动作，评估这个动作对任务的推进程度。

任务目标: {state.goal}

到目前为止的完整历史（已执行的动作及真实观测，按顺序）:
{full_history_text}

即将执行的动作: {action}
该动作预计产生的观测: {predicted_obs}

评估要点:
1. 先根据"完整历史"判断哪些子步骤【已经完成】（例如某个物体是否已被获取、已被处理/加工、已被放置等）。
2. 已经完成的子步骤【不要】再算作"待做"，重复去做它属于原地打转，应评为"无明显进展"。
3. 只有真正推进【尚未完成】的子步骤，或直接完成任务，才算有进展。
4. 到达某个位置若是完成"下一个必要子步骤"的前置，可算"有进展但不足"。

只输出以下格式之一(选一个,加简短理由):
- 任务已完成: [理由]
- 接近完成: [理由]
- 有进展但不足: [理由]
- 无明显进展: [理由]
"""

    task_progress = llm(prompt_step2, client=client, model=model, max_tokens=max_tokens)
    task_progress = task_progress.strip()

    # 拼接两步结果,供 select 使用
    combined = f"观测预测: {predicted_obs}\n任务进度: {task_progress}"
    return combined


def quick_filter_candidates(
    state: WorldModelState,
    all_candidates: List[str],
    target_count: int,
    client: OpenAI,
    model: str,
    max_tokens: int,
    current_think: str = "",
) -> List[str]:
    """
    阶段1：LLM 快速筛选最promising的N个候选（不详细模拟）

    普适设计：只看任务目标、当前思考、最近历史、候选列表，做快速判断
    成本：1次LLM调用
    """
    if len(all_candidates) <= target_count:
        return all_candidates

    # 构建候选列表，并给每个候选标注稀有度（历史出现次数，越小越稀有）。
    # 稀有度是知识库里的一条明文原则，这里把它作为客观数据喂给 LLM 参考。
    counts = state.knowledge.get("action_state_count", {})
    candidates_text = "\n".join(
        [f"{i+1}. {a}  [稀有度={counts.get(a, 0)}]" for i, a in enumerate(all_candidates)]
    )

    # 最近3步历史
    recent = state.observations[-3:] if len(state.observations) > 3 else state.observations
    history_text = "\n".join([f"{a} → {o[:60]}..." for a, o in recent]) if recent else "（初始状态）"

    # 构建 prompt，包含当前思考
    think_section = f"\n当前步思考: {current_think}\n" if current_think else ""

    # 注入知识库里的筛选原则（含稀有度原则）
    principles = state.knowledge.get("filter_principles", [])
    principles_text = "\n".join([f"- {p}" for p in principles]) if principles else "（无）"

    prompt = f"""你是动作筛选专家。根据任务目标、当前思考、最近历史和筛选原则，从候选动作中筛选出最promising的{target_count}个。

任务目标: {state.goal}
{think_section}
最近历史:
{history_text}

筛选原则（请综合运用）:
{principles_text}

候选动作列表（每个动作后的[稀有度=N]表示它在历史上出现过 N 次，N 越小越稀有）:
{candidates_text}

请综合以上原则，选出最可能推进任务的{target_count}个动作。只输出它们的编号（逗号分隔），例如: 3,7,12,15,18,20,22,25,27,28
"""

    output = llm(prompt, client=client, model=model, max_tokens=max_tokens)

    # 解析输出
    try:
        selected_indices = [int(x.strip()) - 1 for x in output.strip().split(",")]
        selected = [all_candidates[i] for i in selected_indices if 0 <= i < len(all_candidates)]
        if len(selected) >= target_count // 2:  # 至少选出一半
            print(f"  快速筛选成功: 选出 {len(selected)} 个候选")
            return selected[:target_count]
    except Exception as e:
        print(f"  快速筛选解析失败: {e}, fallback到分类采样")

    # 解析失败，fallback 到分类采样
    return sample_diverse_candidates(all_candidates, target_count)


def sample_diverse_candidates(actions: List[str], max_count: int) -> List[str]:
    """
    Fallback方案：分类采样保证多样性（普适设计）

    按动作类型（第一个词）分组，每组取几个
    """
    from collections import defaultdict
    import random

    groups = defaultdict(list)
    for act in actions:
        action_type = act.split()[0].lower()
        groups[action_type].append(act)

    sampled = []
    per_group = max(1, max_count // max(1, len(groups)))

    for group_actions in groups.values():
        sampled.extend(random.sample(group_actions, min(per_group, len(group_actions))))

    # 不足 max_count，从剩余随机补
    if len(sampled) < max_count:
        remaining = [a for a in actions if a not in sampled]
        needed = max_count - len(sampled)
        if remaining:
            sampled.extend(random.sample(remaining, min(needed, len(remaining))))

    return sampled[:max_count]


def select_best_action(
    state: WorldModelState,
    candidates: List[Tuple[str, str]],  # [(action, combined_prediction), ...]
    client: OpenAI,
    model: str,
    max_tokens: int,
) -> str:
    """
    LLM 综合判断：看所有 (动作→观测预测+任务进度评估)，选择最优路径

    关键：现在 candidates 里每个预测包含两部分:
      - 观测预测(step1 的幻觉)
      - 任务进度(step2 的评估: 已完成/接近完成/有进展/无进展)
    让 LLM 优先根据任务进度做决策,而不是只看观测文本的"丰富度"
    """
    if not candidates:
        return ""

    # 构建候选列表展示
    candidates_text = ""
    for i, (action, combined_pred) in enumerate(candidates, 1):
        candidates_text += f"{i}. 动作: {action}\n{combined_pred}\n\n"

    prompt = f"""你是一个路径选择专家。根据任务目标,从候选动作及其预测中,选择最可能完成任务的动作。

任务目标: {state.goal}

每个候选包含两部分:
  - 观测预测:执行该动作后可能看到什么
  - 任务进度:该动作对任务目标的推进程度(已完成/接近完成/有进展/无进展)

候选动作及预测:
{candidates_text}

选择规则(按优先级):
1. **优先选"任务已完成"或"接近完成"的动作** — 这是最直接推进任务的路径
2. 若都未完成,选"有进展但不足"的 — 至少在正确方向上
3. 避免选"无明显进展"的 — 那是原地踏步或偏离目标

只输出该动作的编号(1-{len(candidates)}),不要解释。
"""

    output = llm(prompt, client=client, model=model, max_tokens=max_tokens)

    # 解析输出（提取数字）
    try:
        choice = int(output.strip())
        if 1 <= choice <= len(candidates):
            return candidates[choice - 1][0]
    except:
        pass

    # 解析失败，返回第一个
    return candidates[0][0]


def re_review_deadlock(
    state: WorldModelState,
    admissible: List[str],
    stuck_intent: str,
    reason: str,
    client: OpenAI,
    model: str,
    max_tokens: int,
) -> Optional[str]:
    """
    误判死锁重审（完全环境无关）。

    触发时机（由主循环判定）：系统陷入"合理但无法落地的计划"，典型有两类——
      1. 不可执行意图死锁：同一 intent_action 连续多步被提出，却从未真正被执行
         （match 不上、或每次都被 simulate/select 覆盖成瞎逛）。
      2. 幻觉完成死锁：step2 连续多步评"接近完成/已完成"，但 done 始终 False。

    重审的核心是"强制重新落地"：把 (目标 + 完整压缩历史 + 卡住的意图 + 真实可执行列表)
    一起交给 LLM，逼它二选一：
      A. 若意图缺前置条件 → 指出应先做列表里的哪个动作去补齐；
      B. 若意图本身错了 → 从列表里换一个真正不同的可执行动作。
    关键：输出必须是 admissible 里的真实动作（校验编号），否则返回 None 交给兜底。
    不解析任何动作语义，换环境成立。
    """
    if not admissible:
        return None

    # 完整压缩历史（与 step2 同源，供模型判断已做过什么、卡在哪）
    if state.observations:
        full_history = "\n".join(
            [f"{i+1}. 执行: {a} → 观测: {o[:80]}" for i, (a, o) in enumerate(state.observations)]
        )
    else:
        full_history = "（无历史）"

    actions_text = "\n".join([f"{i+1}. {a}" for i, a in enumerate(admissible)])

    prompt = f"""你是一个纠错重规划专家。当前 agent 已陷入死锁：{reason}
它反复坚持一个计划却无法推进任务，很可能是这个计划本身有误，或缺少某个前置步骤。

任务目标: {state.goal}

到目前为止的完整历史（已执行的动作及真实观测，按顺序）:
{full_history}

Agent 一直想做但无法落地的意图: {stuck_intent}

【当前真实可执行的动作列表】（只能从这里选，编号从 1 开始）:
{actions_text}

请判断并二选一：
A. 如果那个意图是对的、只是缺少前置条件（例如需要先拿到某物、先打开某处、
   先去某个位置），就从列表里选出【最能补齐该前置条件】的一个动作。
B. 如果那个意图方向错了（例如该物体不在这里、该交互不存在），就从列表里
   选一个【与最近反复尝试明显不同】的动作，去探索新的可能。

只输出你选择的动作【编号】(1-{len(admissible)})，不要解释。
"""

    output = llm(prompt, client=client, model=model, max_tokens=max_tokens)

    try:
        choice = int(output.strip().split()[0].rstrip(".,)"))
        if 1 <= choice <= len(admissible):
            return admissible[choice - 1]
    except Exception:
        pass
    return None


# ================================================================
# 🔹 知识库更新
# ================================================================

def state_fingerprint(admissible: Optional[List[str]]) -> Optional[frozenset]:
    """
    状态指纹 = 环境提供的可执行动作集合（frozenset）。

    完全零领域知识：代码不解析动作内容，只把这个集合当作当前世界状态的哈希。
    - 世界状态改变（移动、开门、拿取/放下）→ 可执行动作集改变 → 指纹改变。
    - 空操作（如 inventory）→ 可执行动作集不变 → 指纹不变（自环）。
    """
    if admissible is None:
        return None
    return frozenset(admissible)


def rank_by_rarity(
    state: WorldModelState,
    actions: List[str],
) -> List[str]:
    """
    稀有动作优先重排序（完全环境无关，纯频率统计）。

    直觉：一个动作在"历史访问过的状态"里出现得越少，越是"稍纵即逝的机会"——
    它只在少数特定状态下可用（例如某个位置独有的交互），一旦离开就消失；
    而随处可选的动作（如移动到各处）出现频率高，机会随时还在，不必抢着做。
    因此把稀有动作排到前面，让后续筛选/选择更容易选中它们，
    避免"反复探索、却冷落只此一次的关键交互"。

    修复污染：去掉硬过滤后，examine/look 这类噪音动作天然稀有（逐位置生成、
    出现次数少），但它们从不被选中、对任务无意义，却被稀有度优先级推到最前、
    甚至被强制补进候选（filtered_admissible[:2]），挤掉真正推进任务的动作。
    改进：识别"观察类噪音"（出现频次超阈值 且 从未被选中），排序时丢到最后。
    只让"真正稀有的交互动作"参与稀有度排序。完全统计信号，不解析文本。
    """
    counts = state.knowledge.get("action_state_count", {})
    tried = state.knowledge.get("tried_actions", set())

    # 判断某动作是否"观察类噪音"：出现频次 > 5 且从未被选中执行
    # （真正的稀有交互动作要么出现次数少，要么虽常见但会被选中）
    NOISE_THRESHOLD = 5

    selectable = []
    noise = []
    for a in actions:
        freq = counts.get(a, 0)
        if freq > NOISE_THRESHOLD and a not in tried:
            noise.append(a)
        else:
            selectable.append(a)

    # 只对 selectable 部分做稀有度升序排序，noise 扔到最后
    selectable_sorted = sorted(selectable, key=lambda a: counts.get(a, 0))
    return selectable_sorted + noise


def match_intent_to_action(intent: str, admissible: List[str]) -> Optional[str]:
    """
    把模型自己说出的"下一步意图"匹配到某个真实可执行动作（完全环境无关）。

    只做保守的文本匹配，绝不"凑"一个近似动作：
      1. 精确匹配：intent 恰好等于某个候选。
      2. 严格包含：某个候选完整地作为子串出现在 intent 里（intent 常包一层多余的话，
         如 "I will take tomato 1 from countertop 1 now"）。多个候选被包含时取最长者，
         但若最长的有并列则视为歧义，返回 None。
    刻意不做"词重叠/模糊匹配"：那会把区分性最强的词（如物体名 tomato vs apple）
    淹没在公共词（cool/with/fridge/1）里，从而匹配到语义完全不同的动作。
    匹配不上就返回 None，老实走兜底流程，而不是带着高置信度执行一个错动作。
    """
    if not intent or not admissible:
        return None
    intent_l = intent.strip().lower()

    for a in admissible:
        if a.lower() == intent_l:
            return a

    contained = [a for a in admissible if a.lower() in intent_l]
    if not contained:
        return None
    if len(contained) == 1:
        return contained[0]
    contained.sort(key=len, reverse=True)
    # 取最长（信息最具体）；若最长者有并列，则有歧义，交给兜底
    if len(contained[0]) > len(contained[1]):
        return contained[0]
    return None


def update_knowledge_from_observation(
    state: WorldModelState,
    action: str,
    observation: str,
    admissible_before: Optional[List[str]] = None,
    admissible_after: Optional[List[str]] = None,
) -> None:
    """
    从观测更新知识库（运行时学习）

    反向剪枝（完全普适，零领域知识）：识别「无进展」的动作并禁用其在该状态下的重复。

    统一判据 = 「执行动作后，是否到达了一个之前已经访问过的状态」。
    状态用 state_fingerprint（可执行动作集合）表示，不看动作叫什么名字。
    这一条同时覆盖两类问题：
      1. 无效操作（如 inventory）：执行后指纹 == 执行前指纹（自环）→ 原地踏步。
      2. 往返循环（take X 后又 move X back）：放回后指纹变回"拿起之前"的样子
         → 回到来过的状态 → 该"放回"动作无进展。
    误伤防护：若一次往返中真的取得了物品，回到旧位置时手中多出物品，
    环境给出的可执行动作集会不同（多出 put 等），指纹不同 → 不判为 revisit。

    revisit 豁免（解决"到达目标位置→被拉黑→再也回不去"的死锁）：
    即使回到了来过的状态，只要那个状态里仍存在"从未执行过的动作"，
    就说明那里还有没利用的机会（如某处独有的交互还没做），不算无进展、不拉黑。
    判据用集合运算表达：fp_after 中的动作 - 全局 tried_actions ≠ 空。完全环境无关。

    自环的"首次豁免"（关键，解决 cool/heat 类误伤）：
    有些有效动作会改变物体的隐藏属性（如温度），而这种变化不反映在可执行动作集里，
    于是指纹不变、看起来像自环。但它其实推进了任务。为普适地不误伤，采用规则：
    「同一动作在同一状态下的自环，首次执行先放行，只有再次重复时才拉黑。」
    真正的空操作（inventory、对已冷物体反复 cool）会被重复触发从而在第二次被拉黑；
    而首次的有效属性变更动作得以保留。完全不解析动作语义，仅靠"是否重复"判断。

    拉黑的是 (执行前状态指纹, 动作) 组合，而非全局禁用动作字符串：
    同一个动作在别的状态下仍可使用，只在"会导致后退/踏步的那个状态"下被禁。
    """
    fp_before = state_fingerprint(admissible_before)
    fp_after = state_fingerprint(admissible_after)

    if fp_before is not None and fp_after is not None:
        is_self_loop = (fp_after == fp_before)
        is_revisit = fp_after in state.knowledge["visited_states"]
        key = (fp_before, action)

        # revisit 豁免：目标状态仍有"没试过的动作" → 那里还有机会，不算后退。
        # 修复污染：去掉硬过滤后，examine/look 这类"反复出现但从不被选中"的噪音动作
        # 会让 (fp_after - tried_actions) 恒非空，导致豁免永久打开、revisit 剪枝失效。
        # 改进判据：只看那些"至少被选中执行过一次"的动作类型，判断目标状态是否有
        # "新的有交互历史的动作"——只有真正推进任务的动作类型才算"未利用的机会"。
        # 实现：统计"每个动作的首词（动作类型）在历史上是否被选中执行过"。
        # 例如：tried_actions 包含 "take apple 1" → 动作类型 "take" 算有交互历史；
        # examine 类从未被选中 → 不算有交互历史 → 不挡豁免收紧。
        # 完全环境无关、不解析语义，纯统计。
        tried_action_types = set()
        for a in state.knowledge["tried_actions"]:
            parts = a.split()
            if parts:
                tried_action_types.add(parts[0].lower())

        untried_action_types_in_target = set()
        for a in fp_after:
            parts = a.split()
            if parts:
                action_type = parts[0].lower()
                if action_type not in tried_action_types:
                    untried_action_types_in_target.add(action_type)

        # 只有当目标状态存在"新的动作类型（从未被选中执行过的类型）"，才算真机会。
        has_untried_opportunity = bool(untried_action_types_in_target)

        if is_self_loop:
            # 自环首次豁免：同一 (状态, 动作) 自环第一次出现时不拉黑，
            # 记入 seen_self_loops；只有再次出现同一自环才判为真空操作并拉黑。
            if key in state.knowledge["seen_self_loops"]:
                no_progress = True
            else:
                state.knowledge["seen_self_loops"].add(key)
                no_progress = False
            reason = "原地踏步（世界未变，重复）"
        else:
            no_progress = is_revisit and not has_untried_opportunity
            reason = "回到来过的状态（后退）"

        if no_progress and key not in state.knowledge["blacklist"]:
            state.knowledge["blacklist"].add(key)
            print(f"🔒 反向剪枝: 动作无进展[{reason}] '{action}' → {observation[:40]}...")
    else:
        # 兜底：拿不到 admissible 时退化为观测文本比较（自环检测）。
        prev_obs = state.observations[-2][1] if len(state.observations) >= 2 else None
        if prev_obs is not None and observation.strip() == prev_obs.strip():
            key = (None, action)
            if key not in state.knowledge["blacklist"]:
                state.knowledge["blacklist"].add(key)
                print(f"🔒 反向剪枝: 动作无效（观测未变）'{action}' → {observation[:40]}...")


# ================================================================
# 🔹 stuck 检测与处理
# ================================================================

def detect_action_loop(last_actions: List[str], window: int = 6) -> bool:
    """
    检测最近N步的动作是否形成循环模式（完全普适）

    不解析动作内容，只看动作序列是否重复
    例如：[A, B, A, B, A, B] → 循环
         [A, B, C, A, B, C] → 循环
    """
    if len(last_actions) < window:
        return False

    recent = last_actions[-window:]

    # 检测周期为2的循环（最常见）
    if len(recent) >= 4:
        half = len(recent) // 2
        first_half = recent[:half]
        second_half = recent[half:half*2]
        if first_half == second_half:
            return True

    # 检测是否只在少数几个动作之间反复切换
    unique_actions = set(recent)
    if len(unique_actions) <= 2 and len(recent) >= 4:
        # 只有2个不同的动作，反复出现 → 很可能是循环
        return True

    return False


def update_stuck_state(
    stuck_state: StuckState,
    last_actions: List[str],
) -> None:
    """
    更新 stuck 状态（完全普适）

    进入条件（严格）：
    - 检测到动作循环（最近6步在2-3个动作间反复）

    退出条件（放宽）：
    - 进入后持续影响 4 步，强制远离
    """
    # 检测动作循环
    if detect_action_loop(last_actions, window=6):
        if not stuck_state.is_stuck:
            stuck_state.is_stuck = True
            stuck_state.stuck_duration = 4  # 持续影响 4 步
            stuck_state.stuck_actions = list(set(last_actions[-6:]))  # 记录循环中的动作
            print(f"⚠️ 检测到动作循环，强制多样化探索接下来 {stuck_state.stuck_duration} 步")
            print(f"   循环动作: {stuck_state.stuck_actions[:3]}...")

    # stuck 持续倒计时
    if stuck_state.is_stuck:
        stuck_state.stuck_duration -= 1
        if stuck_state.stuck_duration <= 0:
            stuck_state.is_stuck = False
            stuck_state.stuck_actions.clear()
            print("✓ stuck 状态结束")


def filter_actions_if_stuck(
    actions: List[str],
    stuck_state: StuckState,
    last_actions: List[str],
) -> List[str]:
    """
    如果在 stuck 状态，过滤掉最近反复出现的动作（完全普适）

    不解析动作内容，直接过滤掉最近做过的动作本身
    """
    if not stuck_state.is_stuck:
        return actions

    # 过滤掉 stuck_actions 里记录的循环动作
    filtered = [a for a in actions if a not in stuck_state.stuck_actions]

    if filtered:
        print(f"🔄 stuck 过滤: 排除循环动作，剩余 {len(filtered)}/{len(actions)} 个候选")
        return filtered
    else:
        # 全被过滤了，返回原列表（兜底）
        print(f"⚠️ stuck 过滤后无剩余动作，使用原列表")
        return actions


# ================================================================
# 🔹 主循环
# ================================================================

def run_episode(
    env,
    init_prompt: str,
    ob: str,
    info: Dict,
    client: OpenAI,
    model: str,
    max_tokens: int,
    max_steps: int = 50,
) -> Tuple[float, bool, List[StepRecord], int]:
    """
    主循环：think → simulate候选 → select → execute → update_knowledge

    返回: (reward, done, trajectory, llm_call_count)
    """
    prompt = ""
    trajectory: List[StepRecord] = []
    goal = extract_goal(ob)

    # 初始化状态
    state = WorldModelState(goal=goal)
    stuck_state = StuckState()
    last_actions: List[str] = []

    # ---- 误判死锁检测计数器（完全环境无关，纯字符串/计数）----
    # 主信号：同一意图连续多步被提出却从未真正执行 → 不可执行意图死锁
    last_intent: str = ""
    intent_repeat_count: int = 0
    # 辅信号：step2 连续多步评"接近完成/已完成"但 done 仍为 False → 幻觉完成死锁
    optimistic_streak: int = 0
    DEADLOCK_K = 3  # 连续 K 步触发重审

    llm_call_count = 0

    print(f"任务目标: {goal}")
    print(f"最大步数: {max_steps}\n")

    for step in range(1, max_steps + 1):
        print(f"--- Step {step}/{max_steps} ---")

        admissible = get_admissible(info)
        print(f"可执行动作数量: {len(admissible)}")

        if not admissible:
            print("⚠️ 无可执行动作，episode 结束")
            break

        # 更新动作稀有度统计：当前状态里出现的每个动作，出现计数 +1。
        # （在本状态可选 = 又见到了一次；越少见的动作越"稀有"、越像稍纵即逝的机会）
        asc = state.knowledge["action_state_count"]
        for a in admissible:
            asc[a] = asc.get(a, 0) + 1

        # ---- 1. Think（同时给出下一步意图动作）----
        think_prompt = (
            init_prompt + prompt
            + "\n先输出一行以'think:'开头的思路，再输出一行以'action:'开头的、你打算立即执行的具体动作。"
        )
        think_output = llm(think_prompt, client=client, model=model, max_tokens=max_tokens)
        llm_call_count += 1

        think_text, intent_action = "", ""
        for line in think_output.splitlines():
            # 剥掉行首装饰符（少样本里常见的 "> "、"- "、"* "、"# "），再判前缀
            s = line.strip().lstrip(">-*#• ").strip()
            if s.lower().startswith("think:"):
                think_text = s[6:].strip()
            elif s.lower().startswith("action:"):
                intent_action = s[7:].strip()
        if not think_text:
            think_text = think_output.strip()

        trajectory.append(StepRecord(kind="think", text=think_text))
        print(f"思考: {think_text}")

        # ---- 2. 应用反向剪枝过滤 ----
        # 黑名单存 (状态指纹, 动作) 组合：只在"当前状态"下过滤掉已知无进展的动作。
        blacklist = state.knowledge["blacklist"]
        cur_fp = state_fingerprint(admissible)
        filtered_admissible = [act for act in admissible if (cur_fp, act) not in blacklist]

        if not filtered_admissible:
            print("⚠️ 所有动作都在黑名单，使用原始列表")
            filtered_admissible = admissible
        elif len(filtered_admissible) < len(admissible):
            print(f"🔒 反向剪枝过滤: {len(admissible)} → {len(filtered_admissible)}")

        # ---- 3. 应用 stuck 过滤 ----
        filtered_admissible = filter_actions_if_stuck(filtered_admissible, stuck_state, last_actions)

        # ---- 死锁前置判定：当前意图是否与上一步相同（不可执行意图死锁的依据）----
        same_intent_as_last = bool(intent_action) and intent_action == last_intent

        # ---- 4. 意图直采：模型言行一致时，直接执行它说要做的动作 ----
        # think 阶段已给出具体意图动作；只要能匹配到一个真实可执行动作，就直接采用，
        # 跳过 simulate/select（那层在稀疏奖励下常把正确意图覆盖成"到处乱逛"）。
        # stuck 期间不走直采（此时正需要打破模型自己的循环意图）。
        selected_action = None
        if not stuck_state.is_stuck:
            selected_action = match_intent_to_action(intent_action, filtered_admissible)
        took_intent = bool(selected_action)  # 本步是否走"意图直采"（用于死锁计数）
        is_optimistic = False                # 本步所选动作的预测进度是否乐观（仅兜底路径会置位）

        # ---- 4.5 误判死锁重审：意图反复无法落地 / 幻觉式乐观，则强制重新落地 ----
        # 只在"意图未直采成功"时介入（直采成功说明计划正在推进，无需重审）。
        deadlock_reason = ""
        if not took_intent:
            if same_intent_as_last and (intent_repeat_count + 1) >= DEADLOCK_K:
                deadlock_reason = f"同一意图连续 {intent_repeat_count + 1} 步无法落地（不可执行意图死锁）"
            elif optimistic_streak >= DEADLOCK_K:
                deadlock_reason = f"连续 {optimistic_streak} 步评估乐观但任务未完成（幻觉完成死锁）"
        if deadlock_reason:
            print(f"🧭 死锁重审触发: {deadlock_reason}")
            rr = re_review_deadlock(
                state, filtered_admissible, intent_action or last_intent,
                deadlock_reason, client, model, max_tokens,
            )
            llm_call_count += 1
            if rr:
                selected_action = rr
                print(f"🧭 重审改选: {selected_action}")

        if took_intent:
            print(f"🎯 意图直采: {selected_action}  (intent: {intent_action[:40]})")
        elif selected_action:
            pass  # 重审已改选并打印
        else:
            # ---- 兜底：知识库筛选 → 1-step 模拟 → 选择 ----
            # 先按稀有度升序（让候选列表可读，也便于下面取"最稀有"补漏）；
            # 稀有度作为知识库的一条明文原则，已在 quick_filter 内注入并标注给 LLM。
            filtered_admissible = rank_by_rarity(state, filtered_admissible)
            print(f"阶段1: 从 {len(filtered_admissible)} 个候选按知识库原则筛选...")
            candidates_to_simulate = quick_filter_candidates(
                state, filtered_admissible, target_count=10,
                client=client, model=model, max_tokens=max_tokens,
                current_think=think_text,
            )
            llm_call_count += 1
            # 保险：最稀有的动作即使被 LLM 漏掉也强制补进候选，保证机会被评估到
            for a in filtered_admissible[:2]:
                if a not in candidates_to_simulate:
                    candidates_to_simulate.append(a)

            # 🔍诊断：打印意图未命中原因 + 完整可执行集 + 筛选出的候选内容
            # 打印【完整 admissible】：用于判断"意图对应的动作到底在不在环境给的动作里"，
            # 从而区分是"环境没提供该动作"还是"我们自己把对的动作筛掉了"。
            print(f"🔍[诊断] 意图直采未命中, intent_action='{intent_action}'")
            print(f"🔍[诊断] 完整可执行集({len(admissible)}个): {admissible}")
            print(f"🔍[诊断] quick_filter 选出的候选({len(candidates_to_simulate)}个):")
            for c in candidates_to_simulate:
                print(f"      · {c}")

            print(f"阶段2: 模拟 {len(candidates_to_simulate)} 个候选...")
            candidates = []
            for act in candidates_to_simulate:
                pred_obs = simulate_action(state, act, client, model, max_tokens)
                llm_call_count += 2  # 现在是 2-step: 预测观测 + 评估任务进度
                candidates.append((act, pred_obs))

            # 🔍诊断：打印每个候选的模拟预测结果(现在包含观测+任务进度评估)
            print(f"🔍[诊断] 各候选的模拟预测(观测+任务进度):")
            for act, pred in candidates:
                # pred 现在是 "观测预测: ...\n任务进度: ..." 格式
                lines = pred.split('\n', 1)
                obs_part = lines[0][:60] if lines else pred[:60]
                progress_part = lines[1][:60] if len(lines) > 1 else ""
                print(f"      · {act}")
                print(f"        {obs_part}{'...' if len(lines[0]) > 60 else ''}")
                if progress_part:
                    print(f"        {progress_part}{'...' if len(lines[1]) > 60 else ''}")

            selected_action = select_best_action(state, candidates, client, model, max_tokens)
            llm_call_count += 1
            if not selected_action or selected_action not in [c[0] for c in candidates]:
                selected_action = candidates[0][0]

            # 幻觉完成死锁的辅信号：被选中动作的 step2 进度是否"乐观"（接近完成/已完成）。
            # 复用现有 step2 输出，不额外调用 LLM。纯关键词匹配，仅作为死锁触发的辅助计数。
            for act, pred in candidates:
                if act == selected_action:
                    is_optimistic = ("接近完成" in pred) or ("任务已完成" in pred)
                    break

        print(f"选择动作: {selected_action}")
        trajectory.append(StepRecord(kind="act", text=selected_action))
        last_actions.append(selected_action)

        # ---- 6. 执行 ----
        # 记录执行前的可执行动作集（admissible 已在循环开头从 info 取得）
        admissible_before = admissible
        observation, reward, done, info = env.step([selected_action])
        observation = process_ob(observation[0])
        reward = reward[0]
        done = done[0]
        # 执行后的可执行动作集，用于普适地判断动作是否真正改变了世界
        admissible_after = get_admissible(info)

        trajectory.append(StepRecord(kind="ob", text=observation))
        print(f"观测: {observation[:80]}...")
        print(f"奖励: {reward}, 完成: {done}")

        # ---- 7. 更新状态 ----
        state.observations.append((selected_action, observation))
        # 先记录"本步动作已被尝试"，再做剪枝判定：
        # 这样 revisit 豁免里的"目标状态是否还有未尝试动作"判断是自洽的
        # （刚执行过的动作已算 tried，不会把它自己误当成"未利用的机会"）。
        state.knowledge["tried_actions"].add(selected_action)
        # 剪枝判定（此时 visited_states 尚不含本步"执行前状态"，
        # 因此对 fp_after 的 revisit 判断只针对更早访问过的状态）
        update_knowledge_from_observation(
            state, selected_action, observation,
            admissible_before=admissible_before,
            admissible_after=admissible_after,
        )
        # 判定完毕后，将"执行前状态"记入已访问集合（普适：只存指纹，不解析内容）
        fp_before = state_fingerprint(admissible_before)
        if fp_before is not None:
            state.knowledge["visited_states"].add(fp_before)

        # 更新 stuck 状态（基于动作循环检测）
        update_stuck_state(stuck_state, last_actions)

        # ---- 7.5 更新死锁检测计数器 ----
        # 不可执行意图死锁：本步意图与上一步相同则累加；意图变化或成功直采则清零。
        # took_intent（本步成功走了意图直采）说明计划正在推进，也清零。
        if intent_action and same_intent_as_last and not took_intent:
            intent_repeat_count += 1
        else:
            intent_repeat_count = 0
        last_intent = intent_action
        # 幻觉完成死锁：本步走兜底且被选动作评估乐观，但任务未完成 → 累加；否则清零。
        if is_optimistic and not done:
            optimistic_streak += 1
        else:
            optimistic_streak = 0
        # 死锁重审已执行过一次纠偏，重置计数避免连续多步反复重审
        if deadlock_reason:
            intent_repeat_count = 0
            optimistic_streak = 0

        # 更新 prompt（累积历史）
        prompt += f"\n> think: {think_text}\n> {selected_action}\n{observation}\n"

        if done:
            print(f"✓ 任务完成！最终奖励: {reward}")
            print(f"本 episode LLM 调用数: {llm_call_count}")
            return reward, True, trajectory, llm_call_count

    print("达到最大步数，任务未完成")
    print(f"本 episode LLM 调用数: {llm_call_count}")
    return 0.0, False, trajectory, llm_call_count


# ================================================================
# 🔹 结果输出
# ================================================================

def write_results_to_excel(
    rs: List[float],
    cnts: List[int],
    out_path: str,
    method: str = "WorldModel Universal",
    llm_calls_total: int = 0,
    llm_calls_per_task: List[int] = None,
) -> None:
    """输出结果到 Excel"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"

    # 标题
    ws["A1"] = "Method"
    ws["B1"] = method
    ws["A2"] = "Total LLM Calls"
    ws["B2"] = llm_calls_total
    ws["A3"] = "Avg LLM Calls per Task"
    ws["B3"] = f"{llm_calls_total / max(1, sum(cnts)):.1f}"

    # 分类结果
    ws["A5"] = "Task Type"
    ws["B5"] = "Success"
    ws["C5"] = "Total"
    ws["D5"] = "Success Rate"

    task_types = ["put", "clean", "heat", "cool", "examine", "puttwo"]
    for i, (tt, r, c) in enumerate(zip(task_types, rs, cnts), start=6):
        ws[f"A{i}"] = tt
        ws[f"B{i}"] = int(r)
        ws[f"C{i}"] = c
        ws[f"D{i}"] = f"{r/max(1,c):.2%}"

    # 总计
    total_row = 6 + len(task_types)
    ws[f"A{total_row}"] = "Total"
    ws[f"B{total_row}"] = int(sum(rs))
    ws[f"C{total_row}"] = sum(cnts)
    ws[f"D{total_row}"] = f"{sum(rs)/max(1,sum(cnts)):.2%}"

    wb.save(out_path)
    print(f"结果已保存到: {out_path}")


# ================================================================
# 🔹 主函数
# ================================================================

def load_prompts(prompt_file: str) -> Dict[str, str]:
    with open(prompt_file, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    config_file = "base_config.yaml"
    with open(config_file) as f:
        config = yaml.safe_load(f)

    from alfworld.agents.environment.alfred_tw_env import AlfredTWEnv
    env = AlfredTWEnv(config, train_eval="eval_out_of_distribution")
    env = env.init_env(batch_size=1)

    prompt_file = "prompts/alfworld_3prompts.json"
    prompts = load_prompts(prompt_file)

    client = build_client()
    model = os.getenv("OPENAI_MODEL", "qwen3-max-2026-01-23")
    max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))

    prefixes = {
        "pick_and_place": "put",
        "pick_clean_then_place": "clean",
        "pick_heat_then_place": "heat",
        "pick_cool_then_place": "cool",
        "look_at_obj": "examine",
        "pick_two_obj": "puttwo",
    }

    cnts = [0] * 6
    rs = [0] * 6
    llm_calls_total = 0
    llm_calls_per_task = []

    for idx in range(134):
        ob, info = env.reset()
        ob = "\n".join(ob[0].split("\n\n")[1:])
        name = "/".join(info["extra.gamefile"][0].split("/")[-3:-1])

        print(f"\n{'='*60}")
        print(f"任务 {idx+1}/134: {name}")
        print(f"{'='*60}")

        r = 0.0
        for i, (k, v) in enumerate(prefixes.items()):
            if name.startswith(k):
                prompt = (
                    "Interact with a household to solve a task. Here are two examples.\n"
                    + prompts[f"react_{v}_1"]
                    + prompts[f"react_{v}_0"]
                    + "\nHere is the task.\n"
                )
                init_prompt = prompt + ob + "\n>"

                r, done, trajectory, llm_calls = run_episode(
                    env,
                    init_prompt,
                    ob,
                    info,
                    client=client,
                    model=model,
                    max_tokens=max_tokens,
                )

                llm_calls_total += llm_calls
                llm_calls_per_task.append(llm_calls)
                rs[i] += r
                cnts[i] += 1
                break

        overall = sum(rs) / max(1, sum(cnts))
        print(f"\n当前进度: {idx+1}/134")
        print(f"  r={r:.1f} rs={[int(x) for x in rs]} cnts={cnts} avg={overall:.4f}")
        print(f"  总 LLM 调用: {llm_calls_total}, 平均每任务: {llm_calls_total/max(1,sum(cnts)):.1f}")

    # 输出结果
    write_results_to_excel(
        rs, cnts,
        "test4_wm_universal_results-1step.xlsx",
        method="WorldModel Universal (1-step simulate)",
        llm_calls_total=llm_calls_total,
        llm_calls_per_task=llm_calls_per_task,
    )


if __name__ == "__main__":
    main()
