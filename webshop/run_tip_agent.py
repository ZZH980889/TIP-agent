r"""
webshop_pro_search_V4DIAG.py — V4ABCD 的【诊断版】：决策逻辑一行未改，只加成交记录
================================================================================
【本版做到哪一步】V4DIAG = V4ABCD，**决策逻辑逐字节相同**，只增加成交时刻的诊断记录
================================================================================
六版 100-session 实测（每版各跑一次）:
    版本        Avg     Success  步数   LLM   真探
    基线 PRO   0.6002    18     26.0   59.3  51.0
    V3ABC     0.6714    26     19.4   46.0  41.9
    V4ABCD    0.7514    39     15.2   33.6  34.6
    V5ABCEFG  0.6929    31     18.6   43.5  39.5
    V6CONS    0.7591    32     13.7   31.0  32.8
    V6AGGR    0.7435    33     14.4   31.8  33.2
  配对检验（V6 vs V4）: 三组 t 值全部 |t| < 0.6，McNemar 亦不显著
    → **V4/V6CONS/V6AGGR 三版目前分不出来**；V4 的 39% 只有单次运行支撑，
      而它恰好是这批数字里的最高值，最高值天然有向上偏的可能
      （同代码重跑的已知波动：C1 26 vs C2 22，差 4 个点）。
  修复 E/F 的结论（负结果，但依据完整）:
    · F 两轮修复后触发率仍只有 ~3%（V6C 37 个 session 里 1 个、V6A 27 个里 1 个）。
      剩下的变体都是 heather navy vs navy 那类【必须丢修饰词才能匹配、丢了就是
      另一个颜色】的情形 —— 这条路的天花板就在这里。
    · E 触发频繁（33/43 次）且确实消灭了错选类 bug，但代价是 success：
      V6CONS 把 0 分与 <0.3 的尾部压掉 4 个、把 0.7~1 区间从 28 抬到 36，
      而满分从 39 掉到 32。E 让 agent 更稳但更保守 —— 它限制视野避免了灾难性
      错买，也让它错过了账本里那个真正完美的老候选。

【为什么这一版只加日志、不改逻辑】
  F 的两轮修复都是"看着轨迹猜病因、改完再跑"，两次都落空。
  而所有版本都有一批任务【只差 1 个 component】就能满分:
      V4ABCD 差1个=29  → 若全补回 success 39→68
      V6CONS 差1个=34  → 32→66
      V6AGGR 差1个=36  → 33→69
  这是比 E/F 大一个数量级的空间。但现有轨迹只能从分数【反解出差几个】，
  反解不出【差的是哪个】—— 因为 buy 那一刻的核对清单没有和最终 reward 对齐记录。
  所以先做一次纯观测运行，定位清楚再改。

【本版记录什么】
  每个 episode 成交（或耗尽）时，往 Excel 的第二张工作表 "Diagnosis" 写一行:
    session / reward / 分母N（= reward 反解出的 component 总数）/ 差几个 component
    / 成交路径（正常 or 止损）/ 买入的 asin / 商品标题
    / 任务要求的 attrs（键=值，分号分隔）/ price_max
    / 买前核对的 MATCHED / MISSING / UNKNOWN / matched_soft
    / ensure_options 实际补点了什么 / 环境回执里的 options 原文
    / 步数 / LLM 调用数 / 真探步数
  关键的一列是【环境回执里的 options 原文】—— 它是"真的选中了什么"的唯一事实来源，
  把它与任务 attrs 逐条比对，就能算出丢掉的那一条是 color / size / price / attribute。

⚠ 决策逻辑零改动：本文件与 V4ABCD 的差异只有"收集 + 写出"这两类语句，
  不新增任何分支、不改任何判据、不改任何 prompt。因此它同时是 V4ABCD 的重复实验。
================================================================================
================================================================================
【本版做到哪一步】V4 — 改动 A + B + C + D：判据全面对齐奖励函数（完整版）
================================================================================
V1/V2/V3 见上三版说明。本版叠加：

【改动 D】价格改用区间【上界】判定；"仅标题命中、无可点选项"的要求不再计入
         MATCHED；排序键改为以"要求属性的选项覆盖率"为主项。

死因一（价格，轨迹 session_97 的 b09mwm5hck，标价 $10.94 to $34.99）：
  match_item 用 price_low 比预算 → 10.94 <= 30 → 判 PRICE_OK: True。
  但 WebShop 的结算价取决于所选选项，$34.99 那一档超预算 → price 这一条
  直接丢掉（1/N 分）。本版口径：上界在预算内 = 稳过；下界过、上界超 = 有风险
  （进 unknown 并轻罚，让稳过的同级候选优先）；下界都超 = missing。
死因二（假命中）：原版把"颜色词出现在标题里"也算 MATCHED，可是奖励的 option
  条目要求【真的选中过那个选项】。于是一个标题带 navy、但根本没有 color 选项组
  的商品 hard_missing=0，会排在"有真 color 选项但缺 size"的商品前面 ——
  而 hard_missing 是第一排序键。本版把这类记为 matched_soft，不进 MATCHED。
死因三（排序键）：reward ≈ (命中条目数)/(总条目数)，而 option 条目只有真选中
  才算。所以把 chosen_options 的覆盖率提为打分主项。

另含一条无损成本优化（不限制探索范围，只省掉无信息量的 LLM 调用）：
  规则已能定论时短路 step2 —— 要求项全部可落实且价格稳过 → 直接收；
  任务要求具体属性而商品连一个选项组都没有 → 直接弃。

⚠ 探索范围一律不限制：TOPK / 真探次数 / 重搜次数都不封顶（准确率优先）。
================================================================================
================================================================================
血缘：idea3/终版2.py（ALFWorld 上的两阶段世界模型）→ **本文件**
      架构骨架 = 终版2.py 原样（think → 意图直采 → 兜底 step1+step2 → 剪枝/死锁自纠）
      每一个组件都换成 WebShop 领域感知的版本；参照 ../react_webshop_api_run.py 的做法。

================================================================================
零、为什么有这个文件（以及第一版错在哪）
================================================================================
对比算法 ReAct 的 WebShop 实现是【领域专用】的：webshopEnv.step() 里全是字面量
（'Buy Now'/'Back to Search'/'Next >'/'< Prev'）、页面类型硬编码成 init/search/item/
item_sub/end 五种、状态转移写成 assert、allowed_hint_for() 按 page_type 写死自然语言
提示、BeautifulSoup 直接解析 HTML。它可以这么写，因为它不作普适性主张。

既然对比算法用了领域知识，我们也应当有一个同等条件的版本。但要注意 ReAct 的分工：
    **它的领域知识全在【环境侧】；决策侧整个程序只有一行产生动作（第 304 行的 llm 调用），
    搜什么词、点哪个商品、选哪个颜色、何时购买，全部由 LLM 决定。**
    没有打分函数、没有属性匹配、没有排序。

⚠ 本文件第一版犯了两个错误，记录在此以免重犯：
  错误1｜决策也用规则接管了 → 跑完全程【零 LLM 调用】（0.61 分 / 29%）。那不是 Agent，
        是纯规则程序。ReAct 没有给"用规则替 LLM 做决策"开先例。
  错误2｜第二版把 LLM 加回来时，把 终版2.py 的架构换成了"固定五步流水线 + 4 个 LLM
        咨询点"。结果 **没有 think、没有意图直采**，step1/step2 退化成流水线里两个函数
        调用而不是架构层次 —— 它既没有规则版的可靠性，也不再是本方法。
        且成交关口给了 LLM 无预算的否决权，实测连续 SKIP 8 个候选、50 步什么都没买（0 分）。

本版的定位（第三版）：
    **保留 终版2.py 的主循环骨架不动，把它的每一个组件替换成 WebShop 领域感知的版本，
    决策仍然由 LLM 做。** 领域知识的作用是"把每一层喂给 LLM 的信息变准"，不是替它拍板。

================================================================================
一、逐组件对照表：终版2.py 的组件 → 本文件的领域化版本
================================================================================
组件（终版2.py）            本文件的领域化                         借鉴 ReAct 之处
--------------------------  -------------------------------------  ----------------------
extract_goal                parse_instruction：把指令解析成结构化   ReAct 无（它把整条
  （找 "Your task is to:"）  需求（品类词/颜色/尺码/材质/价格上限）   指令塞进 prompt）
                            —— 这是我们相对 ReAct 的净增量
get_admissible              WebShopPro.legal_actions()：页面类型     ReAct 的
                            感知（search 只在搜索页可用）            allowed_hint_for()
                                                                    + assert 状态机
观测文本                     render_observation：[按钮] 包裹 + 每项   ReAct 的 webshop_text
                            换行 + 结果页只详展前 N 个商品            （prod_cnt >= 3 的
                                                                    注意力管理）
match_intent_to_action      land_intent：ASIN 大小写、选项文本精确    ReAct 的
                            匹配、search 作为开放动作放行             normalize_action
【step1】simulate_action     **真探**：真点进商品页 + parse_item_page  ReAct 无前瞻机制
  （LLM 想象预测观测）        拿到【真实选项分组】再退回 —— 颜色/尺码
                            在点进去之前客观不存在于任何可见文本里
【step2】进度评估            输入升级为 match_item 的【结构化属性核对  ReAct 无
  （四档评级）                清单】；LLM 判断品类对不对、缺项要不要紧
quick_filter_candidates     按 score_product_shallow（标题属性命中）  ReAct 无
  （按稀有度）                预排序，决定先真探谁
select_best_action          横向比较结构化清单 + step2 结论           ReAct 无
update_knowledge...         领域规则替代状态指纹（见第二节）           ReAct 用 assert 硬禁
  （反向剪枝）
rank_by_rarity              **删除**：WebShop 没有"稍纵即逝的机会"    —
                            语义（商品列表随时可回），稀有度在这里
                            是噪音。改用属性匹配分排序。
filter_principles           换成 WebShop 的筛选原则                  —
re_review_deadlock          死锁时额外给它结构化需求 + 已核实账本      —
（无）                       新增成交关口：买前逐条核对 + 有预算的     ReAct 直接 buy，
                            "再看一个 or 成交"（见第三节）            无核对

================================================================================
二、反向剪枝为什么必须领域化（这是实测 0 分的直接死因）
================================================================================
终版2.py 的剪枝判据是"状态指纹（=可执行动作集合）相同 ⟹ 回到同一状态"。在 WebShop 上
这个前提两头都不成立：
  · 所有商品详情页的动作集雷同 → 不同商品被判成同一状态
  · **本环境点选项后观测与动作集【完全不变】**（实测 Step 37/38/39 三次观测逐字节相同，
    既无 "You have clicked ..." 也不在页面标记已选项）→ 选项点击被判自环 → 第二次拉黑
    → 该选项从候选里消失 → 已核实的最优商品被丢掉 → 0 分。
所以本文件用三条直白的领域规则替代指纹机制：
  R1 同一商品页上，同一个选项不重复点（已选状态自己记账，环境不给反馈）
  R2 同一个商品的 description/features 子页最多看一次（防子页往返死循环）
  R3 同一个查询不重复发（防反复重搜）
判据直白、不会误伤，而且天然覆盖了指纹机制想防的那些循环。

================================================================================
三、成交关口为什么必须有预算（这是第二版 0 分的直接死因）
================================================================================
第二版问 LLM "买不买"，它每次只看当前一个商品，判断"这个不完美"→SKIP，**每次理由都对**，
但它不知道自己已经否了 8 个、也不知道再否就没了。50 步耗尽、什么都没买。
而不成交 = 0 分，部分匹配 = 0.5 分 —— 反复否决是纯亏。ReAct 的 66 分同样建立在
"实在找不到就买最接近的"之上。
本版把问题改成 **"现在成交，还是先去看还没核实的候选？"** —— 这个问句自带终点：
候选看完了就问不出来了。再叠加两道闸：
  · 否决次数上限 MAX_SKIP（默认 2）
  · 步数将尽时（剩 < CLOSE_RESERVE 步）强制走确定性成交
这正是 思考.txt 第 4 步的原话："如果满了就 buy，如果不满就保留一下这个商品号，
再去找找别的商品" —— "别的"是有限的，这一点必须在代码里表达出来。

================================================================================
四、第 3 步"一口气点击"的正确落地
================================================================================
思考.txt 第 3 步："选好之后用意图直采一口气进行点击商品选择颜色尺码等属性，不要被过度
思考影响"。正确读法是：**点选项这件事本身走意图直采** —— 模型说 click[lake green]，
落地，执行，不进 step1/step2/select 审议层。
领域算法（match_item.chosen_options）算出"该点哪些选项"是**给 think 的提示**，不是代替
它点。第一版把它做成确定性子程序替模型点，那就不是意图直采了。

================================================================================
五、跑法
================================================================================
    python webshop_pro.py                        # 默认 MODE=PRO（本文件主张）
    set WSPRO_MODE=PRO_NOPROBE  & python ...     # 消融：step1 不真探（只看标题）
    set WSPRO_MODE=PRO_NOSTEP2  & python ...     # 消融：关掉 step2 评估
    set WSPRO_MODE=PRO_NOPARSE  & python ...     # 消融：不解析指令（整条指令塞 prompt，≈ReAct）
    可调：WEBSHOP_NUM_EPISODES / WEBSHOP_MAX_STEPS / WSPRO_TOPK / WSPRO_MAX_SKIP
    输出：webshop_pro_results_V4DIAG_<MODE>.xlsx（含 Diagnosis 工作表）

    先导指标（跑 5~10 个 session 就该看）：
      · 成交率必须 ≥ 0.9 —— 低了就是成交关口又在过度否决
      · 每 episode LLM 调用 8~16 次、计入步数 5~12 —— 与 ReAct 同量级
      · 日志里 "意图直采" 应占多数步；"兜底" 只在结果页选商品时出现
"""

# ---- Windows 控制台编码保护（必须在其它 import 之前）----
import sys
for _s in ("stdout", "stderr"):
    _st = getattr(sys, _s, None)
    if _st is not None and hasattr(_st, "reconfigure"):
        try:
            _st.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

import os
import re
import time
from dataclasses import dataclass, field
from fractions import Fraction   # 【诊断】reward 反解
from typing import Dict, List, Optional, Tuple, Any, Set

import openpyxl
from openai import OpenAI

# WebShop 官方 gym 环境延迟到 main() 里 import。
# from web_agent_site.envs import WebAgentTextEnv


# ================================================================
# 🔹 可调常量
# ================================================================

# 版本标识（写进 Excel 的 Method 与输出文件名）
VERSION = "V4DIAG"
VERSION_DESC = "V4ABCD 诊断版（决策逻辑零改动 + 成交记录，兼作 V4 重复实验）"

# 兜底时最多真探几个候选（每个 2 个 env step：点进+点回，不占 max_steps）
# ⚠ 本系列刻意【不封顶】真探与重搜次数：准确率优先，成本如实上报。
TOPK = int(os.getenv("WSPRO_TOPK", "6"))

# 成交关口最多否决几次（见文件头第三节：否决必须有终点）
MAX_SKIP = int(os.getenv("WSPRO_MAX_SKIP", "2"))

# 剩余步数少于此值时，强制走确定性成交（止损保证）
CLOSE_RESERVE = int(os.getenv("WSPRO_CLOSE_RESERVE", "6"))

# 同一查询之外最多改写几次
MAX_REQUERY = int(os.getenv("WSPRO_MAX_REQUERY", "2"))

# 【R2】同一商品的子页最多看几次
MAX_SUBPAGE_PER_ITEM = 1

# 结果页详细展示前几个商品（借鉴 ReAct 的 prod_cnt>=3 注意力管理）
RESULT_DETAIL_N = int(os.getenv("WSPRO_RESULT_DETAIL_N", "5"))

# 连续 K 步意图无法落地 → 死锁重审（沿用 终版2.py）
DEADLOCK_K = 3

# build_query 兜底查询的实词上限。检索词的主要作者是 LLM（由 few-shot 教它怎么写），
# build_query 只在"模型的查询落不了地/重复"时兜底。ReAct 官方演示那条是 6 个实词。
QUERY_MAX_WORDS = int(os.getenv("WSPRO_QUERY_MAX_WORDS", "10"))

MODE_DESC = {
    "PRO":         "领域专用 Agent：think+意图直采+真探step1+结构化step2（本文件主张）",
    "PRO_NOPROBE": "消融：step1 不真探（只看结果页标题，不点进去拿真实选项）",
    "PRO_NOSTEP2": "消融：关掉 step2 评估（拿真实页面+清单直接 select）",
    "PRO_NOPARSE": "消融：不解析指令（整条指令塞 prompt，≈ ReAct 的信息条件）",
}


# ================================================================
# 🔹 数据结构（终版2.py 的三个 + 领域专用的四个）
# ================================================================

@dataclass
class StepRecord:
    """每一步的记录（think / act / ob）—— 终版2.py 原样"""
    kind: str
    text: str


@dataclass
class WorldModelState:
    """
    世界模型状态。骨架取自 终版2.py，但知识库的键换成了 WebShop 领域感知的
    （见文件头第二节：为什么必须换掉状态指纹机制）。
    """
    goal: str = ""
    observations: List[Tuple[str, str]] = field(default_factory=list)
    knowledge: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        k = self.knowledge
        # ---- 终版2.py 保留下来的通用键 ----
        if "tried_actions" not in k:
            k["tried_actions"] = set()
        # ---- 【领域化的反向剪枝】三条规则各自的账本 ----
        if "clicked_options" not in k:
            # R1：{asin: {本次访问已点过的选项}}。本环境点选项后观测不变、无确认信息，
            # 必须自己记账，否则模型看不到生效、会无限重复点同一个选项（实测死因）。
            # 【改动 A】作用域 = 一次商品页访问：离开商品页即清空（见 _sync_option_scope），
            # 因为环境也在那时清空了 options。买入前另有确定性补齐兜底（ensure_options）。
            k["clicked_options"] = {}
        if "subpage_views" not in k:
            k["subpage_views"] = {}      # R2：{asin: 看过几次子页}
        if "queries_issued" not in k:
            k["queries_issued"] = []     # R3：发过的查询（不重复发）
        if "skips_used" not in k:
            k["skips_used"] = 0          # 成交关口已否决几次
        if "filter_principles" not in k:
            # 【领域化】换成 WebShop 的筛选原则（终版2.py 那三条含"稀有度"，
            # 在 WebShop 上是噪音：商品列表随时可回，没有"稍纵即逝的机会"语义）
            k["filter_principles"] = [
                "候选后标注的 [属性命中=N] 是该商品标题与任务要求词的重合数，N 越大越值得先看。",
                "标题里就写明了任务要求的颜色/尺码/材质的商品，优先级最高。",
                "标题品类明显不对的（如任务要女款而标题是男款、要衬衫而是拖鞋），排在后面；但标题措辞与任务不同【不等于】品类错，图案 T 恤/套装类商品的标题常与品类描述不一致。",
                "价格已超出预算上限的商品可以排在后面，但不必完全排除（有时区间下界满足）。",
                "标注了 [已核实] 的候选说明已经真实打开看过，不要重复试探它。",
            ]


@dataclass
class StuckState:
    """stuck 状态追踪 —— 终版2.py 原样"""
    is_stuck: bool = False
    stuck_duration: int = 0
    stuck_actions: List[str] = field(default_factory=list)


# ---- 以下四个是领域专用数据结构 ----

@dataclass
class Requirement:
    """一条可独立判定的需求。"""
    key: str            # "color" / "size" / "price" / "keyword"
    value: str
    hard: bool = True

    def __repr__(self) -> str:
        return f"{self.key}={self.value}"


@dataclass
class ParsedTask:
    """
    【领域知识 1】解析后的结构化任务 —— 取代 终版2.py 的 extract_goal。
    这是相对 ReAct 的净增量：它把整条指令塞进 prompt 让模型自己抠，我们直接算出来。
    """
    raw: str = ""
    price_max: Optional[float] = None
    attrs: Dict[str, str] = field(default_factory=dict)   # 显式键值属性
    keywords: List[str] = field(default_factory=list)     # 品类词 + 修饰词（保序）
    category: str = ""

    def hard_reqs(self) -> List[str]:
        out = [f"{k}={v}" for k, v in self.attrs.items()]
        if self.price_max is not None:
            out.append(f"price<={self.price_max}")
        return out

    def brief(self) -> str:
        """喂给 prompt 的紧凑形态。"""
        return (f"品类相关词: {' '.join(self.keywords) or '（无）'}\n"
                f"  明确要求的属性: {self.attrs or '（无）'}\n"
                f"  价格上限: {self.price_max if self.price_max is not None else '（无）'}")

    def __repr__(self) -> str:
        return (f"ParsedTask(attrs={self.attrs}, price_max={self.price_max}, "
                f"keywords={self.keywords})")


@dataclass
class Product:
    """结果页上的一个商品条目。"""
    asin: str
    title: str = ""
    price_low: Optional[float] = None
    price_high: Optional[float] = None

    @property
    def price(self) -> Optional[float]:
        return self.price_low

    def __repr__(self) -> str:
        return f"{self.asin}({self.title[:26]!r}, ${self.price_low})"


@dataclass
class ItemPage:
    """商品详情页：选项分组 + 标题 + 价格 —— step1 真探的产物。"""
    asin: str = ""
    title: str = ""
    price_low: Optional[float] = None
    price_high: Optional[float] = None
    option_groups: Dict[str, List[str]] = field(default_factory=dict)
    has_buy: bool = False
    subpages: List[str] = field(default_factory=list)

    def all_options(self) -> List[str]:
        return [o for g in self.option_groups.values() for o in g]


@dataclass
class MatchResult:
    """
    【领域知识 3】属性核对结果 —— step2 的【证据】，不是 step2 的结论。
    结论由 LLM 给（见 llm_step2）。
    """
    asin: str = ""
    matched: List[str] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)
    unknown: List[str] = field(default_factory=list)
    # 【改动 D】仅标题写明、但没有对应可点选项的要求（拿不到 option 分）
    matched_soft: List[str] = field(default_factory=list)
    chosen_options: Dict[str, str] = field(default_factory=dict)
    price_ok: Optional[bool] = None
    score: float = 0.0

    @property
    def hard_missing(self) -> int:
        return len(self.missing)

    def unknown_price_risk(self) -> bool:
        """【改动 D】价格区间跨越预算（下界过、上界超）→ 结论未定，仍需 step2。"""
        return self.price_ok is None and any(
            x.startswith("price<=") for x in self.unknown)

    def summary(self) -> str:
        tot = (len(self.matched) + len(self.missing) + len(self.unknown)
               + len(self.matched_soft))
        soft = (f"仅标题命中(无选项可点): {', '.join(self.matched_soft)}\n"
                if self.matched_soft else "")
        return (f"MATCHED: {', '.join(self.matched) or '无'}\n"
                f"MISSING: {', '.join(self.missing) or '无'}\n"
                f"UNKNOWN: {', '.join(self.unknown) or '无'}\n"
                + soft +
                f"MATCH_SCORE: {len(self.matched)}/{tot}\n"
                f"PRICE_OK: {self.price_ok}")


def verdict_category_wrong(verdict: str) -> bool:
    """
    step2 的结论里是否明确判了「品类: 错」。

    ================================================================
    【改动 C】这个判据【不再作硬过滤】，只作排序软惩罚
    ================================================================
    实测病理（session_99，全程 45 步）：
      Step 8 就真探到了 b07pqj2ppz（Disney 图案 T 恤，命中 5、缺失 无），
      step2 判"品类: 错"（标题不是 dress shirt）→ 被硬排除。
      此后 37 步反复重搜 dress shirts，捞回一堆西装外套；45 步耗尽后
      止损路径买下 b07pqj2ppz —— **得分 1.0**。答案第 8 步就在手里。

    根因：WebShop 的指令是从目标商品的属性反向生成的。一件 T 恤的选项里恰好有
    baby blue / women / large，它**就是**目标商品，标题写什么无关。而奖励
    r_type 惩罚在实测 100 个 session 里几乎从不触发（92 个分数用 r_type=1.0
    就能整除解释）。用 LLM 的语义品类判断做硬过滤，是在跟奖励函数对着干。

    另一个代价：硬过滤经常把候选池整个清空（日志"品类全部被判错，放回比较"），
    于是退化成 pool[0]，既没起过滤作用，又白烧了 6 次 step2 调用。
    """
    if not verdict:
        return False
    m = re.search(r"品类\s*[:：]\s*([^\|｜\n]+)", verdict)
    return bool(m and "错" in m.group(1))


@dataclass
class Ledger:
    """已真探过的候选账本（第 4 步"保留商品号"的落地）。"""
    entries: Dict[str, Tuple[ItemPage, MatchResult]] = field(default_factory=dict)
    verdicts: Dict[str, str] = field(default_factory=dict)   # asin → step2 结论

    def add(self, page: ItemPage, res: MatchResult, verdict: str = "") -> None:
        if page.asin:
            self.entries[page.asin] = (page, res)
            if verdict:
                self.verdicts[page.asin] = verdict

    def has(self, asin: str) -> bool:
        return (asin or "").lower() in self.entries

    def category_wrong(self, asin: str) -> bool:
        return verdict_category_wrong(self.verdicts.get((asin or "").lower(), ""))

    def best(self, exclude: Optional[Set[str]] = None,
             allow: Optional[Set[str]] = None) -> Optional[Tuple[ItemPage, MatchResult]]:
        """
        最优候选：先比硬性缺失少、再比分高。
        【BUG3】品类被判错的候选先排除；若排除后为空才放回（不能因为都判错就什么都不买）。
        【BUG2】allow 非 None 时只考虑其中的 asin（= 当前页真能点到的），见 select_best_action。
        """
        ex = exclude or set()
        pool = [(k, v) for k, v in self.entries.items() if k not in ex]
        if allow is not None:
            pool = [(k, v) for k, v in pool if k in allow]
        if not pool:
            return None
        # 【改动 C】品类判错不再排除，只作末位排序键（硬性缺失与匹配分优先）。
        return min(pool, key=lambda kv: (kv[1][1].hard_missing, -kv[1][1].score,
                                        self.category_wrong(kv[0])))[1]

    def render(self, top_n: int = 8) -> str:
        if not self.entries:
            return "（还没有真实核实过任何商品）"
        items = sorted(self.entries.values(),
                       key=lambda kv: (kv[1].hard_missing, -kv[1].score))[:top_n]
        lines = []
        for page, res in items:
            lines.append(f"· {page.asin}  ${page.price_low}  {page.title[:60]}")
            lines.append(f"    命中: {', '.join(res.matched) or '无'}")
            lines.append(f"    缺失: {', '.join(res.missing) or '无'}")
            v = self.verdicts.get(page.asin, "")
            if v:
                lines.append(f"    step2 结论: {v[:120]}")
        return "\n".join(lines)


# ================================================================
# 🔹 LLM 调用（终版2.py 原样）
# ================================================================

def build_client() -> OpenAI:
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.chatanywhere.tech/v1")
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is required")
    return OpenAI(base_url=base_url, api_key=api_key)


def llm(prompt: str, client: OpenAI, model: str, max_tokens: int) -> str:
    for attempt in range(2):
        try:
            r = client.chat.completions.create(
                model=model, messages=[{"role": "user", "content": prompt}],
                stream=False, max_tokens=max_tokens, timeout=60.0)
            return (r.choices[0].message.content or "").strip()
        except Exception as e:
            print(f"LLM调用失败 (attempt {attempt+1}/2): {str(e)[:100]}")
            if attempt == 0:
                time.sleep(2)
                continue
            return ""
    return ""


# ================================================================
# 🔹【领域知识 1】指令解析：取代 终版2.py 的 extract_goal
# ================================================================
# 终版2.py 的 extract_goal 只做一件事：找 "Your task is to:" 后面那行字符串。
# 在 ALFWorld 上够用（目标本来就是一句自然语言指令）；在 WebShop 上不够 ——
# 指令里藏着结构化信息（颜色/尺码/价格上限），而这些信息决定了：
#   · 检索词该保留哪些词（第1步）
#   · 商品页该点哪个选项（第3步）
#   · 买前该核对哪几条（第4步）
# ReAct 不做这件事（把整条指令塞进 prompt 让模型自己抠），所以这是我们的净增量。
# ⚠ 故意不用 LLM 做解析：解析是确定性的，用 LLM 只会引入不确定性和额外调用。

_ATTR_KEYS = (
    "color", "size", "material", "fit", "fit type", "style", "flavor", "scent",
    "pattern", "type", "capacity", "quantity", "count", "width", "length", "wattage",
    "voltage", "finish", "shade", "model", "brand", "package", "pack",
    "item shape", "shape", "special size", "closure", "occasion",
    "heel height", "heel type", "toe shape", "neckline", "sleeve type",
)

_PRICE_PATTERNS = (
    re.compile(r"price\s+(?:lower|less)\s+than\s+\$?([\d.]+)", re.I),
    re.compile(r"(?:under|below|less\s+than|at\s+most|no\s+more\s+than)\s+\$?([\d.]+)", re.I),
    re.compile(r"\$\s*([\d.]+)\s*(?:or\s+less|max|maximum)", re.I),
    re.compile(r"budget\s+(?:of\s+)?\$?([\d.]+)", re.I),
)

# 纯修饰语/停用词：进检索词只会稀释权重
_STOPWORDS = {
    "i", "im", "i'm", "me", "my", "we", "our", "you", "your", "am", "is", "are", "was",
    "be", "been", "a", "an", "the", "and", "or", "but", "if", "of", "for", "to", "in",
    "on", "at", "by", "with", "without", "from", "that", "this", "these", "those", "it",
    "its", "as", "so", "than", "then", "there", "here", "would", "could", "should",
    "will", "shall", "can", "may", "want", "wanted", "wants", "need", "needed", "needs",
    "looking", "look", "like", "please", "also", "some", "any", "very", "really", "get",
    "buy", "find", "searching", "search", "prefer", "preferably", "show", "give",
    "dollars", "dollar", "price", "priced", "cheaper", "lower", "less", "under", "below",
    "cost", "costs", "usd", "one", "something", "item", "product",
    # 属性【键名】本身不进检索词（"in size 9 uk" 里只有 "9 uk" 有区分度，
    # "size" 几乎出现在所有商品标题里）；值仍保留。
    "size", "sizes", "color", "colour", "colors", "material", "style", "quantity",
    "count", "type", "pack", "package", "flavor", "scent", "shade", "finish", "model",
    # 高频出现但几乎不进标题的护理/营销修饰语（实测的稀释源）
    "easy", "care", "machine", "washable", "wash", "tumble", "dry", "quality", "nice",
    "good", "great", "best", "comfortable", "comfy", "officially", "licensed",
    # 实测 session_0 补充：WebShop 指令模板里的"使用场景/体感"标签词。
    # 它们来自商品元数据的 attribute 标签，几乎不出现在【标题】里，进检索词纯稀释。
    # （"day comfort"、"everyday wear"、"daily wear" 都是这类固定短语）
    "day", "comfort", "everyday", "daily", "wear", "occasion", "use", "used",
}

_PRICE_CUE = {
    "under", "below", "lower", "less", "than", "price", "priced", "cost", "costs",
    "most", "max", "maximum", "budget", "cheaper", "within", "dollars", "dollar", "usd",
}
_DECIMAL_RE = re.compile(r"^\$?\d+\.\d+$")
_NUM_RE = re.compile(r"^\$?\d+$")


def parse_instruction(instruction: str) -> ParsedTask:
    """
    把 WebShop 指令解析成结构化需求。三步：抽价格上限 → 抽显式键值属性 → 剩下的当关键词。
    """
    task = ParsedTask(raw=instruction or "")
    text = re.sub(r"^\s*instruction\s*:\s*", "", instruction or "", flags=re.I).strip()

    # ---- 1. 价格上限 ----
    for pat in _PRICE_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                task.price_max = float(m.group(1))
            except ValueError:
                pass
            text = text[:m.start()] + " " + text[m.end():]
            break
    text = re.sub(r"\bdollars?\b", " ", text, flags=re.I)

    # ---- 2. 显式键值属性 ----
    # ⚠ 值可能跨逗号（"color: yellow, star wars comic - kids"），所以捕获到
    # 下一个 ", and [已知键名]:" 为止，而不是到第一个逗号。
    key_alt = "|".join(sorted((k.replace(" ", r"\s+") for k in _ATTR_KEYS),
                              key=len, reverse=True))
    attr_re = re.compile(
        rf"\b({key_alt})\s*:\s*(.*?)(?=\s*,?\s*and\s+(?:{key_alt})\s*:|$)", re.I)
    for m in attr_re.finditer(text):
        key = re.sub(r"\s+", " ", m.group(1).strip().lower())
        val = m.group(2).strip().rstrip(".").strip()
        val = re.sub(r"\s+and\s*$", "", val, flags=re.I).strip().strip(" ,;.").strip()
        if val and key not in task.attrs:
            task.attrs[key] = val.lower()
    text = attr_re.sub(" ", text)

    # ---- 3. 剩余 → 关键词（保序、去停用词、保留尺码/容量数字）----
    raw_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-\.&]*", text)
    norm = [t.lower().strip(".") for t in raw_tokens]
    for i, tl in enumerate(norm):
        if not tl or _DECIMAL_RE.match(tl):
            continue
        if _NUM_RE.match(tl):
            prev = next((norm[j] for j in range(i - 1, -1, -1) if norm[j]), "")
            if tl.startswith("$") or prev in _PRICE_CUE:
                continue
        if tl in _STOPWORDS or tl in task.keywords:
            continue
        task.keywords.append(tl)

    if task.keywords:
        task.category = " ".join(task.keywords[-2:]) if len(task.keywords) >= 2 \
            else task.keywords[-1]
    return task


def build_query(task: ParsedTask, variant: int = 0) -> str:
    """
    按解析结果拼检索词 —— 只作为 LLM 写词时的【建议】与兜底，不代替 LLM 决定。
    变体0：品类词 + 全部具体属性词（主推）；变体1：只关键词；变体2：最宽（品类+2词）。
    依据：词面匹配下多命中一个词就多一份排序权重，而指令里的具体属性词恰恰来自目标商品
    的标题（WebShop 指令是从真实商品反向生成的）。删词等于丢弃最强信号。
    """
    attr_words: List[str] = []
    for k, v in task.attrs.items():
        if k == "price":
            continue
        for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]*", v):
            wl = w.lower()
            if wl not in _STOPWORDS and wl not in attr_words:
                attr_words.append(wl)
    if variant == 0:
        # ⚠ 显式属性词（来自 "with color: X, and size: Y"）必须【保证进入】——
        # 它们取自商品元数据的真实属性值，是最强的排序信号。
        # 早先写成 (keywords + attr_words)[:14]，遇到修饰词特别多的指令（实测那条
        # "slim fit, moisture wicking ... short sleeve, regular fit, long sleeve,
        # stretch fabric, polyester spandex, classic fit"）时，14 个名额被修饰词占满，
        # dark blue / x-large 反而被截掉了 —— 把最该留的丢了。
        kw = [w for w in task.keywords if w not in attr_words]
        budget = max(0, QUERY_MAX_WORDS - len(attr_words))
        return " ".join(kw[:budget] + attr_words)
    if variant == 1:
        return " ".join(task.keywords[:8]) or " ".join(attr_words[:6])
    head = task.keywords[:2]
    tail = task.category.split()
    return " ".join(head + [w for w in tail if w not in head]) or (
        task.keywords[0] if task.keywords else task.raw[:40])


# ================================================================
# 🔹【领域知识 2】页面解析 + 观测重排版
# ================================================================
# 借鉴 ReAct 的 webshop_text（react_webshop_api_run.py:40-105）。它用 BeautifulSoup 解析
# HTML，我们用 WebAgentTextEnv 的 [SEP] 文本 —— 结构同样稳定，可确定性解析。
# 两处关键借鉴：
#   · 按钮用 [] 包裹 + 每项换行（原始 [SEP] 单行里 ASIN/标题/价格/按钮全部同质，
#     模型要靠语义猜哪个能点）
#   · `if prod_cnt >= 3: processed_t = ''` —— 结果页只详展前几个商品，其余只留 [ASIN]。
#     这是注意力管理：10 个标题各 100~200 字符会把"该选哪个"淹没。
#     信息不丢：真探拿的是商品页，不受结果页展示影响。

_BARE_ASIN_RE = re.compile(r"^b0[0-9a-z]{8}$", re.IGNORECASE)
_PRICE_TOKEN_RE = re.compile(r"\$\s*([\d.]+)")

# 商品页上的导航/功能按钮 —— 除这些之外的 click 项都是【选项选择】
_NAV_BUTTONS = {
    "back to search", "< prev", "<prev", "prev", "next >", "next", "buy now",
    "description", "features", "reviews", "attributes",
}
_SUBPAGES = {"description", "features", "reviews", "attributes"}


def _parse_prices(text: str) -> Tuple[Optional[float], Optional[float]]:
    nums = [float(x) for x in _PRICE_TOKEN_RE.findall(text or "")]
    if not nums:
        return None, None
    return min(nums), max(nums)


def parse_results_page(obs: str) -> List[Product]:
    """解析结果页：ASIN → 标题 → 价格 三段一组。"""
    out: List[Product] = []
    if not obs:
        return out
    parts = [p.strip() for p in obs.split("[SEP]")]
    i = 0
    while i < len(parts):
        if _BARE_ASIN_RE.match(parts[i]):
            title = parts[i + 1] if i + 1 < len(parts) else ""
            price_txt = parts[i + 2] if i + 2 < len(parts) else ""
            lo, hi = _parse_prices(price_txt)
            if lo is None:
                lo, hi = _parse_prices(title)
            out.append(Product(asin=parts[i].lower(), title=title,
                               price_low=lo, price_high=hi))
            i += 3
            continue
        i += 1
    return out


def parse_item_page(obs: str, clickables: List[str], asin: str = "") -> ItemPage:
    """
    解析商品详情页，把选项按分组归类 —— 这是 step1 真探的产物，也是本方法相对 ReAct 的
    信息优势所在：颜色/尺码在点进来之前客观不存在于任何可见文本里。
    规则：**不在 clickables 里的短片段是"分组名"，紧随其后的 clickables 属于该分组。**
    （ReAct 用 HTML 的 label 标签判定；我们没有标签，用"是否可点击"区分，等价效果。）
    """
    page = ItemPage(asin=asin)
    if not obs:
        return page
    clickset = {c.strip().lower() for c in (clickables or [])}
    cur_group = ""
    titles: List[str] = []
    for p in [x.strip() for x in obs.split("[SEP]") if x.strip()]:
        pl = p.lower()
        if pl in _NAV_BUTTONS:
            if pl in _SUBPAGES:
                page.subpages.append(pl)
            if pl == "buy now":
                page.has_buy = True
            continue
        if pl.startswith("price"):
            page.price_low, page.price_high = _parse_prices(p)
            continue
        if pl.startswith("rating"):
            continue
        if pl in clickset:
            page.option_groups.setdefault(cur_group or "options", []).append(pl)
        else:
            if len(p) <= 24 and not _PRICE_TOKEN_RE.search(p):
                cur_group = pl
            else:
                titles.append(p)
    if titles:
        page.title = max(titles, key=len)
    if page.price_low is None:
        page.price_low, page.price_high = _parse_prices(obs)
    return page


def render_observation(obs: str, clickables: List[str]) -> str:
    """把 [SEP] 单行观测重排成 ReAct 那种 [按钮]+分行形态；结果页只详展前 N 个商品。"""
    if not obs or "[SEP]" not in obs:
        return obs or ""
    clickset = {c.strip().lower() for c in (clickables or [])}
    lines: List[str] = []
    prod_cnt, suppress = 0, 0
    for p in [x.strip() for x in obs.split("[SEP]")]:
        if not p:
            continue
        if _BARE_ASIN_RE.match(p):
            lines.append(f"[{p}]")
            prod_cnt += 1
            suppress = 2 if prod_cnt > RESULT_DETAIL_N else 0
            continue
        if suppress > 0:
            suppress -= 1
            continue
        lines.append(f"[{p}]" if p.lower() in clickset else p)
    return "\n".join(lines).strip() or obs


# ================================================================
# 🔹【领域知识 3】属性匹配：step2 的【证据】，不是结论
# ================================================================
# 终版2.py 的 step2 输出四档评级（已完成/接近完成/有进展/无明显进展）。在 WebShop 上粒度
# 太粗：实测真探 4 个商品、3 个都评"有进展但不足"，零区分度，select 只能瞎挑。
# 这里用领域算法把"逐条核对"算准，作为证据交给 LLM；结论仍由 LLM 给（见 llm_step2）。

def _norm(s: str) -> str:
    """规范化：小写 + 去非字母数字（x-large≈xlarge，#h4-dark purple≈h4darkpurple）"""
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _tokens(s: str) -> Set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (s or "").lower()) if t}


def value_matches(want: str, candidates: List[str]) -> Tuple[bool, str, float]:
    """
    需求值能否在候选选项里找到？三级匹配（严格度递减）：
      1. 规范化后完全相等   x-large == X-Large == xlarge
      2. 规范化后包含       want=lake green 命中 b4 lake green（选项常带前缀）
      3. 词集覆盖           want 的所有实词都在选项里（顺序无关）
    刻意不做单词级模糊匹配：那会让 green 命中 army green，而 WebShop 评分要求颜色精确 ——
    宁可判不匹配也不要假命中。
    """
    w = _norm(want)
    if not w:
        return False, "", 0.0
    cands = [c for c in (candidates or []) if c]
    for c in cands:
        if _norm(c) == w:
            return True, c, 1.0
    for c in cands:
        if w in _norm(c):
            return True, c, 0.9
    wt = _tokens(want)
    if wt:
        for c in cands:
            if wt <= _tokens(c):
                return True, c, 0.8
    return False, "", 0.0


def match_item(task: ParsedTask, page: ItemPage) -> MatchResult:  # noqa: C901
    """
    逐条核对结构化需求与商品页真实内容。
      · 显式属性：同名分组里找 → 所有选项里找 → 标题里找 → 都没有则 MISSING
        ⚠ "商品没有任何选项"也判 MISSING（不是 UNKNOWN）：任务明确要 color=01-green 而
          商品没有颜色选项，说明它无法满足该属性。早先判 UNKNOWN 导致这类商品显示
          "0 MISSING、分数正数"被选中、带空选项成交，拿 0.0~0.5 分（实测死因）。
      · 价格：与 price_max 比，超标 MISSING
      · 软性关键词：标题里出现 MATCHED，否则 UNKNOWN（标题没写不等于没有）
    产出 chosen_options：每组该点哪个 —— 作为【第3步 think 的提示】，不代替模型点。
    """
    res = MatchResult(asin=page.asin)
    groups = page.option_groups
    title_l = (page.title or "").lower()

    for key, want in task.attrs.items():
        gname = None
        for g in groups:
            if _norm(g) == _norm(key) or _norm(key) in _norm(g) or _norm(g) in _norm(key):
                gname = g
                break
        if gname is not None:
            ok, hit, _ = value_matches(want, groups[gname])
            if ok:
                res.matched.append(f"{key}={want}")
                res.chosen_options[gname] = hit
            else:
                res.missing.append(f"{key}={want}")
            continue
        ok, hit, _ = value_matches(want, page.all_options())
        if ok:
            res.matched.append(f"{key}={want}")
            for g, opts in groups.items():
                if hit in opts:
                    res.chosen_options[g] = hit
                    break
        elif _norm(want) and _norm(want) in _norm(title_l):
            # 【改动 D】只在标题里出现、没有对应可点选项 —— 奖励函数数的是
            # "真的选中过哪些选项"，标题命中拿不到那一条 option 分。
            # 所以记为 soft 命中（不进 matched、不进 missing），并轻罚排序，
            # 让"真有选项可点"的同级候选优先。
            res.matched_soft.append(f"{key}={want}(仅标题写明，无可点选项)")
            res.score -= 0.5
        else:
            res.missing.append(f"{key}={want}")

    # 【改动 D】价格用【区间上界】判定，不用下界。
    # 实测病理（session_97 的 b09mwm5hck，标价 $10.94 to $34.99）：
    #   price_low=10.94 <= 30 → 判 PRICE_OK: True。但 WebShop 的结算价取决于
    #   所选选项，$34.99 那一档超预算 → price 这一条直接丢掉（1/N 分）。
    # 口径：上界就在预算内 = 稳过；下界过、上界超 = 有风险（不算 matched 也不算
    # missing，进 unknown 并轻罚，让稳过的同级候选优先）；下界都超 = missing。
    if task.price_max is not None:
        lo, hi = page.price_low, page.price_high
        if lo is None:
            res.unknown.append(f"price<={task.price_max}")
        elif (hi if hi is not None else lo) <= task.price_max + 1e-9:
            res.matched.append(f"price<={task.price_max}")
            res.price_ok = True
        elif lo <= task.price_max + 1e-9:
            res.unknown.append(
                f"price<={task.price_max}(标价${lo}~${hi}，贵的档位会超预算)")
            res.price_ok = None
            res.score -= 1.0
        else:
            res.missing.append(f"price<={task.price_max}(实际${lo})")
            res.price_ok = False

    covered = _norm(" ".join(task.attrs.values()))
    for w in task.keywords:
        if _norm(w) and _norm(w) in covered:
            continue
        (res.matched if _norm(w) in _norm(title_l) else res.unknown).append(w)

    # 打分只用于【排序展示】，不决定结果（决定权在 LLM）。
    # 选项覆盖度奖励：有对应分组说明"这是个能选颜色/尺码的商品"，起码类型对。
    n_has_group = sum(
        1 for key in task.attrs
        if any(_norm(g) == _norm(key) or _norm(key) in _norm(g) or _norm(g) in _norm(key)
               for g in groups))
    # 【改动 D】排序键对齐奖励函数：reward ≈ (命中条目数)/(总条目数)，而 option 条目
    # 只有"真的选中过"才算。所以 chosen_options（可点且匹配的选项）权重最高。
    n_attr = max(1, len(task.attrs))
    opt_cover = len(res.chosen_options) / n_attr        # 要求属性的选项覆盖率
    res.score += (len(res.matched) * 1.0 - res.hard_missing * 3.0
                  - len(res.unknown) * 0.2 + n_has_group * 0.5
                  + len(res.chosen_options) * 1.5 + opt_cover * 3.0)

    # 【BUG3 修复】品类重合度惩罚。
    # 实测病理：抱枕（Super Soft Throw Pillow）有个 dark mint 颜色选项，被包含匹配判定
    # color=mint 命中；标题里一个任务关键词都没有，却因为 MISSING=0 排在所有真短裤之前。
    # 判据（纯统计、不解析语义）：任务关键词在标题里的重合数。
    #   0 个重合 → 几乎肯定是别的品类（抱枕/海参/音箱线），重罚
    #   只重合 1 个且任务关键词 ≥3 个 → 可疑，轻罚
    # 语义层面的品类判断仍由 step2 的 LLM 做（verdict_category_wrong 是硬过滤）；
    # 这一条只是让排序不至于把明显无关的东西顶到第一。
    if task.keywords:
        overlap = sum(1 for w in task.keywords if _norm(w) and _norm(w) in _norm(title_l))
        if overlap == 0:
            res.score -= 8.0
            res.missing.append("品类不符（标题与任务无任何关键词重合）")
        elif overlap == 1 and len(task.keywords) >= 3:
            res.score -= 2.0
    return res


def score_product_shallow(task: ParsedTask, prod: Product) -> float:
    """
    只看结果页信息（标题+价格）打浅层分 —— 供 quick_filter 决定【先真探谁】。
    这不是最终判据：颜色/尺码在标题里通常看不到，必须点进去（这正是真探的理由）。
    """
    s = 0.0
    t = _norm(prod.title)
    title_l = (prod.title or "").lower()
    for w in task.keywords:
        if _norm(w) and _norm(w) in t:
            s += 1.0
    for v in task.attrs.values():
        if _norm(v) and _norm(v) in t:
            s += 1.5
    if task.price_max is not None and prod.price is not None:
        s += 1.0 if prod.price <= task.price_max else -2.0
    # 性别对立惩罚：任务要女款而标题是男款（或反之）→ 强罚。
    # ⚠ 词界必须加在【前面】：men's 不带前置 \b 会命中 women's 里的 men's，
    #    导致两个分支同时触发、罚分抵消（等于没做）—— 自测已复现此 bug。
    task_text = " ".join(task.keywords).lower()
    W = re.compile(r"\bwomen'?s?\b|\bgirls?\b|\bladies\b", re.I)
    M = re.compile(r"\bmen'?s?\b|\bboys?\b", re.I)
    want_w, want_m = bool(W.search(task_text)), bool(M.search(task_text))
    has_w, has_m = bool(W.search(title_l)), bool(M.search(title_l))
    if want_w and not want_m and has_m and not has_w:
        s -= 4.0
    elif want_m and not want_w and has_w and not has_m:
        s -= 4.0
    return s


def best_option_for(group: str, options: List[str], task: ParsedTask) -> Optional[str]:
    """为一个选项分组算出该点哪个（供 think 提示用）。匹配不上返回 None，不乱点。"""
    for key, want in task.attrs.items():
        if (_norm(group) == _norm(key) or _norm(key) in _norm(group)
                or _norm(group) in _norm(key)):
            ok, hit, _ = value_matches(want, options)
            if ok:
                return hit
    for want in task.attrs.values():
        ok, hit, _ = value_matches(want, options)
        if ok:
            return hit
    return None


# ================================================================
# 🔹 世界模型核心：simulate_action —— 终版2.py 的两阶段，两步都领域化
# ================================================================
# 终版2.py 原版：
#   step1 = LLM 想象"执行该动作后会看到什么"（幻觉预测）
#   step2 = LLM 基于该预测评估任务推进度（输出四档评级）
#
# 在 WebShop 上两步都换掉：
#   step1 → **真探**：真点进商品页、用 parse_item_page 拿到【真实选项分组】、再点回来。
#           理由（这是本方法在 WebShop 上的实质落点）：某商品有没有目标颜色/尺码，
#           在点进去之前【客观地不存在于任何可见文本里】—— 结果页只有标题和价格。
#           给 step1 更多输入救不了它，只能去看。ReAct 完全没有这一层。
#   step2 → 输入升级为 match_item 的【结构化属性核对清单】（不再是四档评级的原始文本）。
#           LLM 的活变成判断"品类对不对、缺的那条要不要紧" —— 这是规则算不出来的部分。
#
# ⚠ 真探不占 max_steps：它走环境内部的 step，不经过主循环计数器（单独统计 probe_steps）。

def simulate_action(
    task: ParsedTask,
    env: "WebShopPro",
    asin: str,
    prod: Optional[Product],
    client: OpenAI,
    model: str,
    max_tokens: int,
    use_probe: bool = True,
    use_step2: bool = True,
) -> Tuple[Optional[ItemPage], Optional[MatchResult], str, int]:
    """
    对一个候选商品做两阶段前瞻。
    返回 (商品页, 核对清单, step2结论, LLM调用数)。真探失败返回 (None, None, "", 0)。
    """
    # ---- step1：后果具体化 ----
    if use_probe:
        page = env.probe_item(asin)          # 领域：点进去 + 解析选项分组 + 点回来
        if page is None:
            return None, None, "", 0
        if prod is not None:
            page.title = page.title or prod.title
            if page.price_low is None:
                page.price_low, page.price_high = prod.price_low, prod.price_high
    else:
        # 消融档：不点进去，只用结果页标题构造一个"伪商品页"（≈ ReAct 的信息条件）
        if prod is None:
            return None, None, "", 0
        page = ItemPage(asin=asin, title=prod.title,
                        price_low=prod.price_low, price_high=prod.price_high)

    # ---- 领域算法产出证据 ----
    res = match_item(task, page)
    res.asin = page.asin

    # ---- step2：LLM 基于证据评估 ----
    if not use_step2:
        return page, res, "", 0
    # 【改动 D】规则已能定论时不调 LLM（这不限制探索范围，只省掉无信息量的调用）：
    #   · 要求的属性全都能在本商品落实、价格稳过 → 结论只能是"值得买"
    #   · 任务要求某属性，而本商品连一个选项组都没有 → 结论只能是"不符"
    if res.hard_missing == 0 and not res.unknown_price_risk():
        return page, res, "VERDICT: 全中 | 品类: 对 | 规则判定：要求项全部可落实（未调用 LLM）", 0
    if task.attrs and not page.option_groups:
        return (page, res,
                "VERDICT: 不符 | 品类: 不确定 | 规则判定：任务要求具体属性，但该商品没有任何可选选项（未调用 LLM）", 0)
    verdict, calls = llm_step2(task, page, res, client, model, max_tokens)
    return page, res, verdict, calls


def llm_step2(task: ParsedTask, page: ItemPage, res: MatchResult,
              client: OpenAI, model: str, max_tokens: int) -> Tuple[str, int]:
    """
    step2：评估该候选对任务的推进度。沿用 终版2.py 的 step2 语义（"这个动作推进任务多少"），
    但输入是真实商品页 + 结构化核对清单，而不是想象预测 + 四档评级。
    LLM 在这里干的是规则干不了的活：判断品类语义、判断 UNKNOWN 项要不要紧。
    """
    opts = {g: v[:12] for g, v in page.option_groups.items()} or "（该商品没有任何可选选项）"
    prompt = f"""你是购物任务的评估专家。下面是一个候选商品【真实打开后看到的内容】（事实，不是猜测）。

任务原文: {task.raw}

候选商品:
  编号: {page.asin}
  标题: {page.title}
  价格: ${page.price_low}
  可选选项: {opts}

按任务要求逐条核对的结果（确定性匹配算出，是事实）:
{res.summary()}

请判断这个候选值不值得买。评分规则是【逐条累加】的：满足的条目越多分越高，
所以要判断的是"这个商品能落实几条要求"，而不是"它是否完美"。重点考虑:
1. **可选选项能不能覆盖任务要求的颜色/尺码/版型** —— 这是最重要的一条，
   因为得分直接数"选中了几个要求的选项"。有对应选项 = 这一条能拿到分。
2. MISSING 里的项，是"商品客观没有这个选项"，还是"只是标题没写"？后者往往仍能满足。
3. UNKNOWN 里的项（如 machine wash / imported / classic fit）通常写在
   [features] 或 [description] 子页里，标题不写不等于不满足。
4. 标题品类只在【它明确排除了任务品类】时才算问题（如任务要鞋而这是抱枕）。
   ⚠ 注意：本商店的商品标题经常与品类描述不一致（图案 T 恤的标题写的是图案主题、
   家具套装的标题写的是单件名）。**如果它的选项恰好覆盖了任务要求的颜色和尺码，
   那它很可能就是目标商品**，不要因为标题措辞不同就判它品类错。

只输出一行:
VERDICT: <全中|接近|部分|不符> | 品类: <对|错|不确定> | 一句话理由
"""
    return (llm(prompt, client, model, max_tokens) or "").strip(), 1


# ================================================================
# 🔹 quick_filter：终版2.py 的阶段1，判据换成领域属性匹配
# ================================================================
# 终版2.py 按【稀有度】排序（动作在历史状态里出现越少 → 越像"稍纵即逝的机会"）。
# 那个语义在 WebShop 上不成立：商品列表随时可以回来，没有"错过就没了"这回事，
# 稀有度在这里纯粹是噪音。改用 score_product_shallow（标题属性命中数）排序，
# 决定【先真探谁】—— 真探有成本，顺序很重要。

def quick_filter_candidates(
    task: ParsedTask,
    products: List[Product],
    ledger: Ledger,
    target_count: int,
    client: OpenAI,
    model: str,
    max_tokens: int,
    current_think: str,
    principles: List[str],
) -> Tuple[List[Product], int]:
    """
    从结果页商品里筛出最值得真探的 N 个。领域打分先排序，再让 LLM 定最终名单
    （LLM 能看出"这标题是男款/是拖鞋"这类规则看不出的品类错位）。
    返回 (候选列表, LLM调用数)。
    """
    fresh = [p for p in products if not ledger.has(p.asin)]
    if not fresh:
        return [], 0
    ranked = sorted(fresh, key=lambda p: -score_product_shallow(task, p))
    if len(ranked) <= target_count:
        return ranked, 0

    lines = []
    for i, p in enumerate(ranked, 1):
        hit = score_product_shallow(task, p)
        lines.append(f"{i}. {p.asin}  ${p.price_low}  [属性命中={hit:.1f}]  {p.title[:70]}")
    prompt = f"""你是购物候选筛选专家。要从结果页的商品里挑出最值得【真实打开查看】的 {target_count} 个
（打开一个是有成本的，所以顺序和取舍很重要）。

任务原文: {task.raw}
结构化要求:
  {task.brief()}

当前这一步的思考: {current_think or '（无）'}

筛选原则:
{chr(10).join('- ' + x for x in principles)}

候选商品（已按属性命中数预排序，顺序不代表结论）:
{chr(10).join(lines)}

只输出你要打开查看的 {target_count} 个的编号，逗号分隔（如 1,3,5,7），不要解释。
"""
    out = llm(prompt, client, model, max_tokens)
    try:
        idxs = [int(x.strip()) - 1 for x in (out or "").strip().split(",")]
        picked = [ranked[i] for i in idxs if 0 <= i < len(ranked)]
        if len(picked) >= max(1, target_count // 2):
            return picked[:target_count], 1
    except Exception:
        pass
    return ranked[:target_count], 1


# ================================================================
# 🔹 select_best_action：终版2.py 的选择层，看结构化清单横向比较
# ================================================================

def select_best_action(
    task: ParsedTask,
    ledger: Ledger,
    exclude: Set[str],
    client: OpenAI,
    model: str,
    max_tokens: int,
    clickable_now: Optional[Set[str]] = None,
) -> Tuple[Optional[str], int]:
    """
    从已真探的候选里选一个（终版2.py 的 select_best_action 在 WebShop 上的落点）。
    领域打分只用于排序展示；结论由 LLM 给。返回 (asin 或 None, LLM调用数)。

    ⚠【BUG2 修复】clickable_now = 当前页面真能点到的 ASIN 集合。
    早先这里从【整个账本】挑，于是反复选中第 1 页探到的抱枕 b09h2pm436，而当时已经翻到
    第 2/3 页 —— click 打在当前页不存在的 ASIN 上被环境静默忽略，实测白烧 5 步。
    账本里其它页的候选不是不能用，但要用得先导航回去；这里只在"现在就能点"的范围内选。
    ⚠【BUG3 修复】品类被判错的候选整体排除；全排除则放回（否则无可买）。
    """
    pool = [(k, v) for k, v in ledger.entries.items() if k not in exclude]
    if clickable_now is not None:
        pool = [(k, v) for k, v in pool if k in clickable_now]
    if not pool:
        return None, 0
    # 【改动 C】已有候选逐条核对无硬性缺失 → 直接选它，不必再问 LLM。
    # 奖励是逐条累加的：无缺失就是当前可达的上限，比较只会引入品类语义噪音。
    _clean = [(k, v) for k, v in pool if v[1].hard_missing == 0]
    if _clean:
        _clean.sort(key=lambda kv: -kv[1][1].score)
        print(f"   ✅ 有 {len(_clean)} 个候选无硬性缺失，直接选最高分的 {_clean[0][0]}")
        return _clean[0][0], 0
    # 【改动 C】不再按品类判错做硬过滤（那会把正确答案删掉，见 verdict_category_wrong）。
    # 品类只作【末位排序键】：硬性缺失少、匹配分高的优先，同分时才让品类判断说话。
    _nwrong = sum(1 for k, _ in pool if ledger.category_wrong(k))
    if _nwrong:
        print(f"   ℹ {_nwrong}/{len(pool)} 个候选被 step2 判'品类错'，作降级排序而非排除")
    pool.sort(key=lambda kv: (kv[1][1].hard_missing, -kv[1][1].score,
                             ledger.category_wrong(kv[0])))
    if len(pool) == 1:
        return pool[0][0], 0

    lines = []
    for i, (asin, (page, res)) in enumerate(pool, 1):
        opts = {g: v[:8] for g, v in page.option_groups.items()} or "（无任何选项）"
        lines.append(f"{i}. {asin}  ${page.price_low}  {page.title[:66]}")
        lines.append(f"     命中: {', '.join(res.matched) or '无'}")
        lines.append(f"     缺失: {', '.join(res.missing) or '无'}")
        lines.append(f"     未知: {', '.join(res.unknown) or '无'}")
        lines.append(f"     可选选项: {opts}")
        v = ledger.verdicts.get(asin, "")
        if v:
            lines.append(f"     step2: {v[:130]}")
    prompt = f"""你是购物决策专家。下面每个候选都已经【真实打开看过】，内容是事实。

任务原文: {task.raw}
结构化要求:
  {task.brief()}

候选（已按缺失少/匹配多预排序，顺序不代表结论）:
{chr(10).join(lines)}

选择规则（按优先级）:
1. **缺失项越少越好** —— 得分是逐条累加的（满足条目数 / 总条目数），
   所以缺失最少的那个就是当前可达的最高分。
2. 缺失项相同时，选提供了任务所需选项（颜色/尺码可点）的那个：
   "没有任何选项"的商品无法选中指定颜色尺码，一定丢掉那几条分。
3. 仍相同时选命中最多的。
4. 品类只在【明确排除】时才作为否决理由（任务要鞋而这是抱枕/海参/音箱线）。
   ⚠ 标题措辞与任务不同【不等于】品类错：图案 T 恤的标题写图案主题、家具套装的
   标题写单件名，这很常见。**选项覆盖了任务要求的颜色和尺码，就是强烈的正向信号。**

只输出你选择的商品编号（形如 b08dk7s9b1），不要解释。
"""
    out = llm(prompt, client, model, max_tokens)
    m = re.search(r"b0[0-9a-z]{8}", out or "", re.I)
    if m and m.group(0).lower() in dict(pool):
        return m.group(0).lower(), 1
    return pool[0][0], 1


# ================================================================
# 🔹 think：终版2.py 主循环的第 1 步，prompt 领域增强
# ================================================================
# 终版2.py 的 think prompt = init_prompt（含 few-shot）+ 累积历史 + "输出 think:/action:"。
# 领域增强的部分（全部是"把信息摆准"，不替它做判断）：
#   · 结构化需求清单（parse_instruction 的产物）
#   · 当前页面的重排版观测（render_observation，借鉴 ReAct 的 webshop_text）
#   · 当前页面的合法动作清单（借鉴 ReAct 的 allowed_hint_for）
#   · 已核实事实备忘（Ledger —— 避免反复回去核实系统早已核实过的商品）
#   · 已点过的选项（本环境点选项无确认信息，不给它记账它必然重复点）
#   · 领域算法算出的"该点哪些选项"（第3步的提示，不代替它点）

# ---------------------------------------------------------------
# 【BUG5 修复】few-shot 演示：ReAct 官方 WebShop 轨迹（deodorant）
# ---------------------------------------------------------------
# 早先本文件【完全没有 few-shot】—— 而 llm_think 的注释里我自己写着
# "终版2.py 的 think prompt = init_prompt（含 few-shot）+ 累积历史"，实现时丢了。
# 后果（实测 session_0）：模型只能照文字规则猜检索词，把 day comfort / everyday wear /
# machine washable / easy care 这些【几乎不出现在商品标题里】的护理营销词全塞进查询
# （8~10 个词），召回全是连体裤、瑜伽裤、裙子。而 ReAct 演示那条只有 6 个词、全是具体属性。
#
# ⚠ 这段是 ReAct 原文，think 文字与动作序列【逐字未改】—— 它是 62 分那版唯一被验证过的
# 素材。webshop5 曾擅自加 plan: 行、重写 Notes，实测掉到 40.9 分。不要再改它。
# 唯一的改动是【观测行排版与运行时对齐】：改成 render_observation 输出的 [按钮]+分行形态
# （原文是 [SEP] 单行）。示例观测必须与模型实际看到的一致，否则示范作用被削弱。
WEBSHOP_FEWSHOT = r"""下面是一次成功购物的完整示范（观测格式与你实际看到的一致）：

Instruction: i would like a 3 ounce bottle of bright citrus deodorant for sensitive skin, and price lower than 50.00 dollars
> think: Search the core product plus the most distinctive features only; leave price out of the query.
> search[3 ounce bright citrus deodorant sensitive skin]
[Back to Search]
Page 1 (Total results: 50)
[Next >]
[B078GWRC1J]
Bright Citrus Deodorant by Earth Mama | Natural and Safe for Sensitive Skin, Pregnancy and Breastfeeding, Contains Organic Calendula 3-Ounce
$10.99
[B078GTKVXY]
Ginger Fresh Deodorant by Earth Mama | Natural and Safe for Sensitive Skin, Contains Organic Calendula 3-Ounce
$10.99
> think: B078GWRC1J is a bright citrus deodorant for sensitive skin under $50 and matches the category. Open it.
> click[b078gwrc1j]
[Back to Search]
[< Prev]
scent
[assorted scents]
[bright citrus]
[calming lavender]
[ginger fresh]
[simply non-scents]
size
[travel set (4-pack)]
[3 ounce (pack of 1)]
[3-ounce (2-pack)]
Bright Citrus Deodorant by Earth Mama 3-Ounce
Price: $10.99
Rating: N.A.
[Description]
[Features]
[Reviews]
[Buy Now]
> think: The task wants bright citrus scent and a 3 ounce bottle. I must click the option text exactly as listed: scent 'bright citrus' and size '3 ounce (pack of 1)'. Click the scent first.
> click[bright citrus]
You have clicked bright citrus.
> think: Now select the exact size the task asks for.
> click[3 ounce (pack of 1)]
You have clicked 3 ounce (pack of 1).
> think: Both required options (bright citrus, 3 ounce (pack of 1)) are selected and the price is under $50. Buy it now.
> click[buy now]

从这个示范里要学到的（尤其是检索词）:
- 检索只用【品类词 + 任务点明的具体属性词】。上例保留了 3 ounce / bright citrus /
  deodorant / sensitive skin，共 6 个实词，去掉了价格。
- **不要把护理与营销类词放进检索词**：machine washable / easy care / tumble dry /
  day comfort / everyday wear / moisture wicking 这类词几乎不出现在商品标题里，
  放进去只会稀释真正有用的词，把无关品类捞上来。
- 也不要缩到 1-2 个通用词（如 search[shorts]），那会返回一堆热门无关商品。
- 商品页的选项形如 "scent" 后跟 [bright citrus] 等；要点【与任务文字完全一致】的那个
  （如 click[3 ounce (pack of 1)]，不是 click[3-ounce (2-pack)]）。
- 结果页只详细展示前几个商品的标题，其余只有编号 —— 编号一样可以点。
- 所有必需选项都选好之后再 click[buy now]。购买不可逆。

⚠ 上面这个例子有一点【不代表一般情况】，务必注意:
  这个例子里目标商品的标题恰好写出了 scent 和 size（Bright Citrus … 3-Ounce），
  所以看标题就能判断。**但绝大多数任务不是这样：颜色和尺码几乎从不出现在标题里，
  它们是商品页【里面】的选项。**
  例如任务要 color: acorn / size: 30w x 34l，结果页上不会有任何标题写着 acorn 或
  30w x 34l —— 但点进 "Wrangler Men's Relaxed Fit Jean" 之后，选项里就有它们。
  因此:
  - 用标题判断【品类】对不对（jeans / shorts / polo / 抱枕），这个标题能看出来。
  - 颜色、尺码、版型这些【必须点进商品页才能知道】，不要因为标题没写就否掉它。
  - **不要因为"这一页标题里没有我要的颜色/尺码"就翻页或重新搜索** —— 那是必然的，
    翻到第 5 页也一样。第 1 页只要有品类对的商品，就应该打开它看选项。
"""


def llm_think(
    task: ParsedTask,
    env: "WebShopPro",
    ledger: Ledger,
    history: str,
    client: OpenAI,
    model: str,
    max_tokens: int,
    hint: str = "",
) -> Tuple[str, str, int]:
    """返回 (think文本, 意图动作, LLM调用数)。"""
    legal = env.legal_actions()
    legal_txt = "\n".join(f"  {a}" for a in legal[:40]) or "  （本页没有可点击的按钮）"
    if len(legal) > 40:
        legal_txt += f"\n  …（共 {len(legal)} 个，只列前 40 个）"
    # 【BUG1 修复】search 的可用性单独说明，不作为可选项混进 legal 列表
    if env.search_available():
        legal_txt += ("\n  search[...] —— 本页有搜索框，你可以自己写关键词，"
                      "例如 search[women's mint shorts]")

    memo = ""
    if ledger.entries:
        memo = ("\n【已核实事实备忘】下面这些商品已经真实打开看过（系统替你看的，看完已退回）。\n"
                "这些是事实，它们现在仍然可选。不要再花步数导航回去核实它们。\n"
                + ledger.render() + "\n")

    picked = ""
    if env.page_type == PAGE_ITEM:
        done_opts = env.clicked_options_for(env.cur_asin)
        live = env.item_page
        should = match_item(task, live).chosen_options
        todo = {g: o for g, o in should.items() if o not in done_opts}
        picked = (f"\n【选项状态】本商品页你已经点过: {sorted(done_opts) or '（还没点）'}\n"
                  f"  ⚠ 本商店点选项后【页面文字完全不变、也没有确认信息】—— 这是正常的，"
                  f"你的点击已经生效，不要因为看不到变化就重复点同一个选项。\n"
                  f"  按任务要求还应该点: {todo or '（都点完了，可以购买）'}\n"
                  f"  注：离开本商品页（回结果页/重新搜索）会让已选选项失效，重新进来需要重新点。\n")

    prompt = f"""你在一个关键词检索的购物网站上完成购物任务。每一步只能执行一个动作。

{WEBSHOP_FEWSHOT}
========== 下面是你要完成的任务 ==========

任务原文: {task.raw}
从任务里解析出的结构化要求（事实，供你参考）:
  {task.brief()}

最近历史:
{history or '（刚开始）'}

当前页面:
{env.render()}
{picked}{memo}
【当前这一步真正可执行的动作】（你的 action 必须是其中之一）:
{legal_txt}
{hint}
要点:
- 检索时保留品类词和任务点明的具体属性词（颜色/尺寸/材质等），去掉价格数字、以及护理与
  营销类词（machine washable / easy care / tumble dry / day comfort / everyday wear
  这些几乎不出现在商品标题里，放进去只会稀释有用的词）。也不要缩到 1-2 个通用词。
- 结果页只详细展示了前几个商品的标题，其余只有编号 —— 编号一样可以点。
- **颜色和尺码几乎从不出现在标题里，它们是商品页里面的选项。** 所以：用标题判断【品类】
  对不对；颜色/尺码/版型必须点进商品页才知道。不要因为"这一页标题里没写我要的颜色或
  尺码"就翻页或重新搜索 —— 翻到第 5 页也一样看不到。第 1 页只要有品类对的商品就打开它。
- 进入商品页后，逐个点选任务要求的选项，全部选好再购买。购买不可逆。
- 任务要求的属性在标题和选项里都看不出来时（如 imported zipper / machine wash /
  long lasting 这类），点 [features] 或 [description] 确认 —— 这两个子页各可以看一次。

先输出一行以'think:'开头的思路，再输出一行以'action:'开头的、你打算立即执行的具体动作。
"""
    out = llm(prompt, client, model, max_tokens)
    think_text, intent = "", ""
    for line in (out or "").splitlines():
        s = line.strip().lstrip(">-*#• ").strip()
        low = s.lower()
        if low.startswith("think:"):
            think_text = s[6:].strip()
        elif low.startswith("action:"):
            intent = s[7:].strip()
    if not think_text:
        think_text = (out or "").strip()
    # 借鉴 ReAct 的 normalize_action：从自由文本里抠出动作
    if not intent:
        m = re.search(r"(search\[[^\]]*\]|click\[[^\]]*\])", out or "", re.I)
        if m:
            intent = m.group(1)
    return think_text, intent, 1


# ================================================================
# 🔹 意图落地：终版2.py 的 match_intent_to_action，领域化
# ================================================================
# 终版2.py 只做保守文本匹配（精确相等 / 完整子串包含）。领域化加三条：
#   · ASIN 大小写不敏感（观测里是大写 B08DK7S9B1，clickables 里是小写）
#   · search[...] 是开放动作：本页有搜索框就放行原文（借鉴 ReAct 让 LLM 自由写 query）
#   · 选项文本要精确匹配 clickables（避免发出环境不接受的近似文本）

def land_intent(intent: str, legal: List[str], search_ok: bool = False) -> Optional[str]:
    """
    把模型说的意图落地到一个真实可执行动作。落不了地返回 None（老实走兜底）。
    search_ok 由 env.search_available() 给出 —— search 是开放动作，不在 legal 里
    （见 legal_actions 的 BUG1 说明）。
    """
    if not intent:
        return None
    s = intent.strip()
    sl = s.lower()

    # search 是开放动作：本页有搜索框就原文放行；且必须有真实关键词
    if sl.startswith("search["):
        if not search_ok:
            return None
        kw = s[s.find("[") + 1:s.rfind("]")].strip() if "[" in s and "]" in s else ""
        # 防御：模型可能把 prompt 里的占位提示原样抄回来
        if not kw or kw.startswith("<") or "关键词" in kw or "your own" in kw.lower():
            return None
        return s

    # 精确匹配（大小写不敏感）
    for a in legal:
        if a.lower() == sl:
            return a
    # ASIN：容忍大小写与 click[] 包裹
    m = re.search(r"b0[0-9a-z]{8}", sl, re.I)
    if m:
        want = f"click[{m.group(0).lower()}]"
        for a in legal:
            if a.lower() == want:
                return a
    # 完整子串包含（模型常包一层多余的话）
    contained = [a for a in legal if a.lower() in sl]
    if len(contained) == 1:
        return contained[0]
    if len(contained) > 1:
        contained.sort(key=len, reverse=True)
        if len(contained[0]) > len(contained[1]):
            return contained[0]
    return None


# ================================================================
# 🔹【领域化的反向剪枝】三条规则，替代 终版2.py 的状态指纹机制
# ================================================================
# 为什么必须换（文件头第二节）：终版2.py 判"指纹（=动作集）相同 ⟹ 回到同一状态"。
# WebShop 上两头都不成立：所有商品页动作集雷同；而且【点选项后观测与动作集完全不变】，
# 于是选项点击被判自环、第二次拉黑，把唯一正确的下一步永久删掉（实测 0 分死因）。
# 三条领域规则直白、不会误伤，且覆盖了指纹机制想防的循环：

def apply_domain_pruning(state: WorldModelState, env: "WebShopPro",
                         legal: List[str]) -> Tuple[List[str], List[str]]:
    """
    返回 (过滤后的合法动作, 被剪掉的原因说明)。
      R1 同一商品页上，同一个选项不重复点（已选状态自己记账 —— 环境不给反馈）
      R2 同一个商品的 description/features 子页最多看 MAX_SUBPAGE_PER_ITEM 次
      R3 同一个查询不重复发
    """
    out, cut = [], []
    clicked = env.clicked_options_for(env.cur_asin)
    sv = state.knowledge["subpage_views"]
    for a in legal:
        m = re.match(r"^click\[\s*(.+?)\s*\]$", a.strip(), re.I)
        txt = (m.group(1).strip().lower() if m else "")
        # R1：已点过的选项（非导航项）不再列出
        if txt and txt not in _NAV_BUTTONS and not _BARE_ASIN_RE.match(txt) \
                and txt in clicked:
            cut.append(f"R1 已点过的选项: {a}")
            continue
        # R2：该商品的【这个】子页看够了（按 (商品,子页名) 计数，见 env.step 的说明）
        if txt in _SUBPAGES:
            n = sv.get((env.cur_asin, txt), 0)
            if n >= MAX_SUBPAGE_PER_ITEM:
                cut.append(f"R2 [{txt}] 已看过 {n} 次: {a}")
                continue
        out.append(a)
    return (out or legal), cut


def is_repeat_query(state: WorldModelState, query: str) -> bool:
    """R3：这个查询是否已经发过（防反复重搜）。"""
    q = re.sub(r"\s+", " ", (query or "").strip().lower())
    return q in {re.sub(r"\s+", " ", x.strip().lower())
                 for x in state.knowledge["queries_issued"]}


# ================================================================
# 🔹 stuck 检测：终版2.py 原样（纯序列重复，环境无关，这层不必领域化）
# ================================================================

def detect_action_loop(last_actions: List[str], window: int = 6) -> bool:
    if len(last_actions) < window:
        return False
    recent = last_actions[-window:]
    if len(recent) >= 4:
        half = len(recent) // 2
        if recent[:half] == recent[half:half * 2]:
            return True
    return len(set(recent)) <= 2 and len(recent) >= 4


def update_stuck_state(stuck_state: StuckState, last_actions: List[str]) -> None:
    if detect_action_loop(last_actions, window=6):
        if not stuck_state.is_stuck:
            stuck_state.is_stuck = True
            stuck_state.stuck_duration = 4
            stuck_state.stuck_actions = list(set(last_actions[-6:]))
            print(f"⚠️ 检测到动作循环，强制多样化 {stuck_state.stuck_duration} 步")
    if stuck_state.is_stuck:
        stuck_state.stuck_duration -= 1
        if stuck_state.stuck_duration <= 0:
            stuck_state.is_stuck = False
            stuck_state.stuck_actions.clear()
            print("✓ stuck 状态结束")


# ================================================================
# 🔹 死锁重审：终版2.py 原样 + 领域信息（结构化需求 + 已核实账本）
# ================================================================

def re_review_deadlock(
    task: ParsedTask, env: "WebShopPro", ledger: Ledger, legal: List[str],
    stuck_intent: str, reason: str, history: str,
    client: OpenAI, model: str, max_tokens: int,
) -> Tuple[Optional[str], int]:
    """
    终版2.py 的死锁重审：把 (目标 + 历史 + 卡住的意图 + 真实可执行列表) 交给 LLM，
    逼它二选一（补前置条件 / 换一个真正不同的动作）。
    领域增强：额外给它结构化需求与已核实账本 —— 卡住时最有用的信息往往是
    "我早就核实过某个候选还没成交"。
    """
    if not legal:
        return None, 0
    memo = ("\n已核实过的商品（这些是事实，仍然可选）:\n" + ledger.render()
            if ledger.entries else "")
    actions_text = "\n".join(f"{i+1}. {a}" for i, a in enumerate(legal))
    prompt = f"""你是纠错重规划专家。当前购物 agent 已陷入死锁：{reason}
它反复坚持一个做法却无法推进，很可能这个做法本身有误，或缺少某个前置步骤。

任务原文: {task.raw}
结构化要求:
  {task.brief()}

最近历史:
{history or '（无）'}
{memo}
它一直想做但无法落地的意图: {stuck_intent}

当前页面: {env.page_type}
【当前真实可执行的动作】（只能从这里选，编号从 1 开始）:
{actions_text}

请判断并二选一：
A. 若那个意图是对的、只是缺前置条件（例如想搜索但当前不在搜索页 → 需要先
   click[back to search]），就选出【最能补齐该前置条件】的动作。
B. 若那个意图方向错了，就选一个【与最近反复尝试明显不同】的动作去打破僵局。

只输出你选择的动作【编号】(1-{len(legal)})，不要解释。
"""
    out = llm(prompt, client, model, max_tokens)
    try:
        choice = int(re.search(r"\d+", out or "").group())
        if 1 <= choice <= len(legal):
            return legal[choice - 1], 1
    except Exception:
        pass
    return None, 1


# ================================================================
# 🔹 成交关口：终版2.py 没有这一层，是 WebShop 专属
# ================================================================
# 思考.txt 第 4 步："选择结束后评估当前选项的匹配度，如果满了就 buy，如果不满就保留一下
# 这个商品号，再去找找别的商品"。
# ⚠ 关键教训（文件头第三节）：问"买不买"会让 LLM 无预算地连续否决（实测 SKIP 8 次、
#   50 步什么都没买、0 分）。改成问 **"现在成交，还是先去看还没核实的候选？"** ——
#   这个问句自带终点：候选看完了就问不出来了。再叠加否决次数上限与步数储备两道闸。
# 【改动 B】再往前一步：否决必须以一个【具体的、浅层分更高的未核实候选】为前提，
#   且'无硬性缺失'直接成交、不问 LLM。理由见 commit_gate 的 docstring。

def commit_gate(
    task: ParsedTask, page: ItemPage, final: MatchResult, selected: Dict[str, str],
    unchecked: List[Product], skips_used: int, steps_left: int,
    client: OpenAI, model: str, max_tokens: int,
    ledger: Optional["Ledger"] = None,
) -> Tuple[bool, str, int]:
    """
    返回 (是否成交, 理由, LLM调用数)。

    ================================================================
    【改动 B】否决从"LLM 自由裁量"改为"规则判定 + 必须有更好的候选"
    ================================================================
    实测病理（session_97）：LOOK 的收益是负的。
      · 否决后固定 click[< prev] 回结果页；
      · think 看到备忘写着"该商品已核实、仍然可选"，于是又点回同一个商品；
      · land_intent 完全不看 skipped，拦不住；
      · 于是 MAX_SKIP 被消耗成两轮 4~6 步空转，且每次往返都让已选选项失效（改动 A）。
    全程 38 次否决，没有一次换来更好的商品，只换走了步数和选项。

    所以本版把问句从"你觉得这个够好吗"换成一个**可判定的事实问题**：
        "未真探的候选里，有没有一个浅层分严格高于当前商品的？"
    没有 → 直接成交（不调 LLM）。有 → 才交给 LLM 定夺，并把那个更好的候选摆给它看。
    这样"否决"必须以一个具体的、更有希望的替代品为前提，而不是以"当前这个不完美"为前提。
    """
    # ---- 结构性直接成交（不调 LLM）----
    if not unchecked:
        return True, "没有未核实的候选了，不成交只会 0 分 → 成交", 0
    if skips_used >= MAX_SKIP:
        return True, f"否决次数已达上限 {MAX_SKIP} → 强制成交", 0
    if steps_left < CLOSE_RESERVE:
        return True, f"剩余步数仅 {steps_left}，来不及再看 → 成交", 0

    # ---- 【改动 B】规则闸：当前商品已无硬性缺失 → 直接成交 ----
    # 硬性缺失为 0 意味着任务要求的属性都能在这个商品上落实（选项存在或标题写明），
    # 而奖励是逐条累加的：这种情况下换商品的期望收益不为正，只有步数与选项失效的风险。
    if final.hard_missing == 0:
        return True, "逐条核对无硬性缺失 → 直接成交（无需再看）", 0

    # ---- 【改动 B】规则闸：未核实候选里没有更有希望的 → 直接成交 ----
    cur_shallow = score_product_shallow(
        task, Product(asin=page.asin, title=page.title,
                      price_low=page.price_low, price_high=page.price_high))
    better = [p for p in unchecked if score_product_shallow(task, p) > cur_shallow + 1e-9]
    if not better:
        return True, (f"未核实候选的浅层匹配分都不高于当前商品"
                      f"（当前 {cur_shallow:.1f}）→ 成交"), 0
    better.sort(key=lambda p: -score_product_shallow(task, p))

    opts = {g: v[:10] for g, v in page.option_groups.items()} or "（无）"
    cand_txt = "\n".join(
        f"  - {p.asin} ${p.price_low} [属性命中={score_product_shallow(task, p):.1f}] {p.title[:60]}"
        for p in better[:6])
    prompt = f"""你即将执行【不可逆】的购买。这是唯一决定得分的一步。

任务原文: {task.raw}
结构化要求:
  {task.brief()}

当前商品:
  编号: {page.asin}
  标题: {page.title}
  价格: ${page.price_low}
  全部可选选项: {opts}
  将要选中的选项: {selected or '（这个商品没有任务需要的选项）'}

逐条核对结果:
{final.summary()}

下面是【尚未核实、且标题属性命中数严格高于当前商品】的候选（当前商品命中={cur_shallow:.1f}）:
{cand_txt}
（还可以否决 {MAX_SKIP - skips_used} 次；剩余步数 {steps_left}）

请二选一:
BUY  —— 当前商品缺的那些项，上面这些候选大概率也满足不了；或者它们的品类明显更差。
LOOK —— 上面某个候选明显更可能满足当前商品缺失的那几项。

⚠ 重要事实（务必据此判断）:
- 不成交得 0 分；部分匹配按"满足的条目数 / 总条目数"给分，所以买下一个缺一两项的商品远好过不买。
- 离开这个商品页会让【已经选好的颜色/尺码失效】，回来必须重新点 —— 否决的成本不是 0。
- 只有当你能指出上面某个具体候选更可能补上缺失项时才 LOOK。仅仅"当前这个不完美"不是理由。

只输出一行:
DECISION: <BUY|LOOK> | 一句话理由
"""
    out = llm(prompt, client, model, max_tokens) or ""
    up = out.upper()
    if "LOOK" in up and "BUY" not in up.split("LOOK")[0]:
        return False, out.strip(), 1
    return True, out.strip(), 1


# ================================================================
# 🔹【领域知识 4】环境包装：页面状态机（借鉴 ReAct 的 webshopEnv）
# ================================================================
# ReAct 把页面类型硬编码成 init/search/item/item_sub/end 五种，用 assert 表达状态转移
# （search 只能从 init 发、Buy Now 只能在 item 页、Next > 直接 assert False）。
# 我们照这个思路做，但基于 WebAgentTextEnv 而非 HTTP，且【不用 assert 阻断】——
# 改为向上层暴露 legal_actions()，让上层不发非法动作（更接近 ALFWorld 的 admissible 语义，
# 也让 终版2.py 的意图直采/兜底链路能原样工作）。

PAGE_SEARCH, PAGE_RESULTS, PAGE_ITEM, PAGE_SUB, PAGE_END = \
    "search", "results", "item", "item_sub", "end"


class WebShopPro:
    """WebShop 专用环境包装：维护页面类型、解析页面内容、提供领域动作与真探能力。"""

    def __init__(self, env, state: Optional[WorldModelState] = None):
        self.env = env
        self.state = state              # 用于记账（已点选项/子页次数/查询历史）
        self.instruction = ""
        self.task: ParsedTask = ParsedTask()
        self.page_type = PAGE_SEARCH
        self.cur_asin = ""
        self.last_obs = ""
        self.products: List[Product] = []
        self.item_page: ItemPage = ItemPage()
        self.probe_steps = 0            # 真探消耗（不计入 max_steps）
        self.env_steps = 0              # 计入 max_steps
        # 【改动 A】当前正在访问的商品（clicked_options 的作用域锚点）。
        # 环境在离开商品页后会清空已选 options，账本必须跟着清，否则 R1 会
        # 把'其实已经失效'的选项永久拉黑（实测 session_97 的 options {} 死因）。
        self._visit_asin = ""
        # 【诊断】仅用于记录：最近一次 ensure_options 补点了什么
        self.diag_last_fill: List[str] = []

    # ---- 基础 ----
    def _clickables(self) -> List[str]:
        try:
            av = self.env.get_available_actions()
            return [str(c) for c in (av.get("clickables", []) or [])] \
                if isinstance(av, dict) else []
        except Exception:
            return []

    def _has_search_bar(self) -> bool:
        try:
            av = self.env.get_available_actions()
            return bool(av.get("has_search_bar", False)) if isinstance(av, dict) else False
        except Exception:
            return False

    def _strip(self, raw: str) -> str:
        """剥掉每页重复的 'WebShop [SEP] Instruction: [SEP] <整条指令> [SEP]' 前缀。"""
        if not raw or "[SEP]" not in raw or "Instruction:" not in raw:
            return raw or ""
        parts = [p.strip() for p in raw.split("[SEP]")]
        while parts and parts[0].lower() == "webshop":
            parts.pop(0)
        if parts and parts[0].lower().startswith("instruction:"):
            parts.pop(0)
            if parts:
                parts.pop(0)
        return (" [SEP] ".join(parts)).strip() or raw

    def render(self) -> str:
        """当前页面的重排版观测（给 think 看）。"""
        return render_observation(self.last_obs, self._clickables())

    def legal_actions(self) -> List[str]:
        """
        当前页面【真能原样执行】的动作 —— 对应 终版2.py 的 get_admissible。

        ⚠【BUG1 修复】早先这里在搜索页塞了一个字面量占位符 "search[<关键词由你自己写>]"，
        本意是告诉模型"这里能搜索"。但兜底路径的 filtered[0] 恰好就是它，于是被【原样发给
        环境】—— 实测 Step 9/17 搜了字面量 "<关键词由你自己写>"，返回音箱线、海参、红枣，
        六步全废还污染了账本。
        现在这个列表里只放真能执行的动作；search 是开放动作（关键词由模型自己写），
        它的可用性单独由 search_available() 声明，绝不作为可选项混进来。
        """
        out: List[str] = []
        for c in self._clickables():
            cl = c.strip().lower()
            if cl == "search":
                continue
            out.append(f"click[{cl}]")
        return out

    def search_available(self) -> bool:
        """本页是否有搜索框。search[...] 的关键词由模型自己写，故不进 legal_actions。"""
        return self.page_type == PAGE_SEARCH or self._has_search_bar()

    def clicked_options_for(self, asin: str) -> Set[str]:
        if self.state is None:
            return set()
        return self.state.knowledge["clicked_options"].setdefault((asin or "").lower(), set())

    def _sync(self, raw_obs: str):
        """执行完动作后刷新页面类型与解析结果。"""
        self.last_obs = self._strip(raw_obs)
        low = self.last_obs.lower()
        clk = self._clickables()
        clk_l = {c.strip().lower() for c in clk}
        if "thank you for shopping" in low:
            self.page_type = PAGE_END
        elif self._has_search_bar():
            self.page_type = PAGE_SEARCH
        elif "buy now" in clk_l:
            self.page_type = PAGE_ITEM
            self.item_page = parse_item_page(self.last_obs, clk, asin=self.cur_asin)
        elif any(_BARE_ASIN_RE.match(c.strip()) for c in clk):
            self.page_type = PAGE_RESULTS
            self.products = parse_results_page(self.last_obs)
        else:
            self.page_type = PAGE_SUB
        self._sync_option_scope()

    def _sync_option_scope(self) -> None:
        """【改动 A】把 clicked_options 的作用域对齐到"本次商品页访问"。"""
        if self.state is None:
            return
        book = self.state.knowledge["clicked_options"]
        if self.page_type == PAGE_SUB:
            return                      # 子页往返不清空（实测选项仍然有效）
        if self.page_type == PAGE_ITEM:
            if self._visit_asin and self._visit_asin != self.cur_asin:
                book.pop(self._visit_asin, None)
            self._visit_asin = self.cur_asin
            return
        # 结果页 / 搜索页 / 结束页：本次访问结束，环境已清空 options
        if self._visit_asin:
            book.pop(self._visit_asin, None)
            self._visit_asin = ""

    # ---- 生命周期 ----
    def reset(self, session: int) -> str:
        try:
            raw, _ = self.env.reset(session=session)
        except TypeError:
            raw = self.env.reset(session=session)
        for attr in ("instruction_text", "goal"):
            v = getattr(self.env, attr, None)
            if isinstance(v, str) and v.strip():
                self.instruction = v.strip()
                break
        self.task = parse_instruction(self.instruction)
        self.page_type, self.cur_asin = PAGE_SEARCH, ""
        self.products, self.item_page = [], ItemPage()
        self.probe_steps = self.env_steps = 0
        self._visit_asin = ""
        self._sync(raw if isinstance(raw, str) else str(raw))
        return self.last_obs

    def step(self, action: str, count: bool = True) -> Tuple[str, float, bool]:
        """
        执行一个动作（领域记账在这里做）。count=False 表示真探内部步，不计入 max_steps。
        """
        al = action.strip().lower()
        m = re.match(r"^click\[\s*(.+?)\s*\]$", al)
        txt = m.group(1).strip() if m else ""

        # 记账：查询历史 / 已点选项 / 子页次数（领域剪枝 R1~R3 的数据来源）
        if self.state is not None:
            if al.startswith("search["):
                self.state.knowledge["queries_issued"].append(action[7:-1])
            elif txt in _SUBPAGES:
                # 【R2 修复】按 (商品, 子页名) 计数，不再按商品共享一个额度。
                # 早先写成 k[asin]：模型看过 features 想再看 description 时被一并拦掉，
                # 而它是对的 —— features 里确实写着 Imported / Zipper closure /
                # Machine Wash 这些任务要求的 attribute。拦掉直接压住了满分上限
                # （实测四个硬属性全中却只得 0.875，差的就在这些 attribute 上）。
                k = self.state.knowledge["subpage_views"]
                key = (self.cur_asin, txt)
                k[key] = k.get(key, 0) + 1
            elif txt and txt not in _NAV_BUTTONS and not _BARE_ASIN_RE.match(txt):
                self.clicked_options_for(self.cur_asin).add(txt)

        if txt and _BARE_ASIN_RE.match(txt):
            self.cur_asin = txt
        if al.startswith("search["):
            self.cur_asin = ""

        raw, reward, done, _ = self.env.step(action)
        if count:
            self.env_steps += 1
        else:
            self.probe_steps += 1
        self._sync(raw)
        return self.last_obs, float(reward), bool(done)

    # ================================================================
    # 【改动 A】买入前的确定性选项补齐
    # ================================================================
    def ensure_options(self, task: ParsedTask,
                       trace: Optional[List["StepRecord"]] = None) -> List[str]:
        """
        在 click[buy now] 之前，把任务要求的选项【确定性地】补点齐。返回实际补点的动作。

        为什么必须有这一层（实测病理，session_97）：
          Step 17/18 点了 x-large 与 brown，Step 19 被成交关口否决 → click[< prev]
          → 重进商品页 → 又被否决 → 重进 → Step 23 强制成交。
          成交回执是 options {} —— 两个选项都没生效，只拿到 0.333 分。
          原因是 clicked_options 是 episode 级账本、只增不减：
            · R1 把"已点过"的选项从 legal 里删掉 → 模型点不了
            · think 的选项提示写着"（都点完了，可以购买）" → 模型不想点
          而环境在离开商品页后把 options 清空了。账本与环境状态脱钩，分数就在这里漏掉。

        为什么这样修是安全的：**重复点一个已经选中的选项在本环境没有副作用**，
        所以这里不去猜"环境到底在哪些跳转上清空 options"，而是在买之前无条件对齐一次。
        奖励函数数的是"真的选中过哪些选项"，不是"我以为点过哪些"。
        """
        acted: List[str] = []
        if self.page_type != PAGE_ITEM:
            return acted
        want = match_item(task, self.item_page).chosen_options
        clicked = self.clicked_options_for(self.cur_asin)
        for g, o in want.items():
            if o in clicked:
                continue
            self.step(f"click[{o}]")
            acted.append(o)
            if trace is not None:
                trace.append(StepRecord(kind="act", text=f"click[{o}]"))
            print(f"   🅐 补齐选项 [{g}] → click[{o}]")
            if self.page_type != PAGE_ITEM:      # 极端情况：点击把页面带走了，停手
                break
        if not acted:
            print(f"   🅐 选项已齐（{sorted(want.values()) or '本商品无需选项'}）")
        # 【诊断】把本次补点的内容留在环境上，供成交时记录（不影响任何判断）
        self.diag_last_fill = list(acted)
        return acted

    def probe_item(self, asin: str) -> Optional[ItemPage]:
        """
        【step1 真探】点进商品页拿真实选项清单，再点回结果页。不计入 max_steps。
        安全性：只在结果页发起；回退后核对是否真的回到了结果页，否则重发查询恢复。
        """
        if self.page_type != PAGE_RESULTS:
            return None
        saved_asin = self.cur_asin
        self.step(f"click[{asin}]", count=False)
        if self.page_type != PAGE_ITEM:
            if self.page_type != PAGE_RESULTS:
                self.step("click[< prev]", count=False)
            self.cur_asin = saved_asin
            return None
        page = self.item_page
        self.step("click[< prev]", count=False)
        if self.page_type != PAGE_RESULTS:
            qs = self.state.knowledge["queries_issued"] if self.state else []
            if qs:
                print("⚠️ 真探回退后不在结果页，重发上次查询恢复")
                self.step(f"search[{qs[-1]}]", count=False)
        self.cur_asin = saved_asin
        return page


# ================================================================
# 🔹 主循环 —— 终版2.py 的骨架，逐层领域化
# ================================================================
# 终版2.py 的骨架（一字不改地保留）：
#   for step: think → 剪枝过滤 → stuck 过滤 → 意图直采 → 死锁重审 → 兜底(step1+step2+select)
#             → 执行 → 更新知识库
# 领域化的落点：
#   · think        prompt 里给结构化需求/重排版观测/合法动作/已核实账本/已选选项
#   · 剪枝          三条领域规则（R1 选项不重复点 / R2 子页最多一次 / R3 查询不重复）
#   · 意图直采      land_intent（ASIN 大小写、search 开放动作、选项精确匹配）
#   · 让位于真探    只在结果页选商品时让位（这是本方法的贡献所在，其余不让位）
#   · 兜底 step1    真探（拿真实选项分组）
#   · 兜底 step2    LLM 基于结构化清单评估
#   · 成交关口      有预算的"成交 or 再看一个"
#   · 止损保证      步数将尽 → 确定性成交（保住规则版的可靠性）

def _diag_capture(diag: Dict[str, Any], env: "WebShopPro", task: ParsedTask,
                  obs: str, reward: float, path: str) -> None:
    """
    【诊断】成交时刻的事实采集。不参与任何判断。
    最关键的是 env_options —— 环境回执里的 options 原文，是"真的选中了什么"的
    唯一事实来源。把它与任务 attrs 逐条比对，才能算出丢掉的那条是谁。
    """
    diag["path"] = path
    diag["reward"] = float(reward)
    diag["fill"] = "; ".join(getattr(env, "diag_last_fill", []) or [])
    diag["task_attrs"] = "; ".join(f"{k}={v}" for k, v in task.attrs.items())
    diag["price_max"] = task.price_max
    diag["task_keywords"] = " ".join(task.keywords)
    m = re.search(r"options\s*\[SEP\]\s*(\{.*?\})", obs or "", re.S)
    diag["env_options"] = m.group(1) if m else ""
    m2 = re.search(r"asin\s*\[SEP\]\s*([A-Za-z0-9]+)", obs or "")
    if m2:
        diag["env_asin"] = m2.group(1).lower()
    if not diag.get("buy_asin"):
        diag["buy_asin"] = env.cur_asin
        diag["buy_title"] = (env.item_page.title or "")[:120]
        diag["buy_price"] = env.item_page.price_low
    # reward = 命中条目数 / 总条目数 → 反解分母与差几个
    if reward and 0 < reward <= 1:
        f = Fraction(reward).limit_denominator(20)
        if abs(float(f) - reward) < 2e-3:
            diag["n_total"] = f.denominator
            diag["n_short"] = f.denominator - f.numerator


def run_episode(
    env: WebShopPro,
    client: OpenAI,
    model: str,
    max_tokens: int,
    max_steps: int = 50,
    mode: str = "PRO",
    diag: Optional[Dict[str, Any]] = None,
) -> Tuple[float, bool, List[StepRecord], int, int]:
    """
    返回 (reward, done, trajectory, llm_calls, probe_steps)。

    【诊断】diag 是一个由调用方传入的空 dict，本函数往里塞成交时刻的事实。
    它不参与任何判断，只被 write_results 读走。
    """
    if diag is None:
        diag = {}
    use_probe = (mode != "PRO_NOPROBE")
    use_step2 = (mode != "PRO_NOSTEP2")
    no_parse = (mode == "PRO_NOPARSE")

    state = WorldModelState(goal=env.instruction)
    env.state = state
    task = env.task
    if no_parse:
        # 消融：不解析指令（≈ ReAct 的信息条件：整条指令塞 prompt，模型自己抠）
        task = ParsedTask(raw=env.instruction)
    stuck_state = StuckState()
    ledger = Ledger()
    trajectory: List[StepRecord] = []
    last_actions: List[str] = []
    history = ""
    llm_calls = 0

    # 终版2.py 的死锁计数器
    last_intent, intent_repeat, optimistic_streak = "", 0, 0
    # 被让位给真探的意图（改动 F2）
    deferred_intent = ""
    # 成交关口否决过的候选
    skipped: Set[str] = set()
    # 【改动 B】被否决后暂时挂起的候选：真探过任一新候选后解禁（见兜底分支）
    suspended: Set[str] = set()

    print(f"指令: {env.instruction}")
    print(f"解析: {task}")
    print(f"硬性要求: {task.hard_reqs()}")
    print(f"最大步数: {max_steps}  模式: {mode}\n")

    for step in range(1, max_steps + 1):
        steps_left = max_steps - step + 1
        print(f"--- Step {step}/{max_steps} [{env.page_type}] ---")

        legal = env.legal_actions()
        search_ok = env.search_available()
        # 【BUG1 配套】搜索页的 legal 是空的（唯一动作是开放式 search），不能据此结束
        if not legal and not search_ok:
            print("⚠️ 无可执行动作，结束")
            break

        # ---- 1. think ----
        hint = ""
        if env.page_type == PAGE_RESULTS and ledger.entries:
            hint = ("\n提示：备忘里已有核实过的商品；若其中已有满足要求的，直接点它的编号；"
                    "若都不合适，可以 click[next >] 翻页或 click[back to search] 重新检索。\n")
        think_text, intent, _c = llm_think(task, env, ledger, history,
                                          client, model, max_tokens, hint=hint)
        llm_calls += _c
        trajectory.append(StepRecord(kind="think", text=think_text))
        print(f"思考: {think_text[:150]}")

        # ---- 2. 领域剪枝过滤 ----
        filtered, cut = apply_domain_pruning(state, env, legal)
        # 【改动 B】被挂起的候选本步不可点（防止刚否决就点回去）
        if suspended and env.page_type == PAGE_RESULTS:
            _f = [a for a in filtered
                  if not any(a.lower() == f"click[{s}]" for s in suspended)]
            if _f != filtered:
                print(f"🚦 挂起过滤: {sorted(suspended)} 本步不可点")
                filtered = _f or filtered
        if cut:
            print(f"🔒 领域剪枝: {len(legal)} → {len(filtered)}  ({cut[0]})")

        # ---- 3. stuck 过滤 ----
        if stuck_state.is_stuck:
            _f = [a for a in filtered if a not in stuck_state.stuck_actions]
            if _f:
                print(f"🔄 stuck 过滤: 剩余 {len(_f)}/{len(filtered)}")
                filtered = _f

        same_intent = bool(intent) and intent == last_intent
        selected: Optional[str] = None
        took_intent = False
        is_optimistic = False

        # ---- 4. 意图直采 ----
        if not stuck_state.is_stuck:
            selected = land_intent(intent, filtered, search_ok=search_ok)
            # 【BUG4 修复】R3 从"硬禁"降为"软约束"：重复查询不再直接拦掉。
            # 早先拦下之后没有合理去处，直接掉进 filtered[0] —— 而那正是 BUG1 的占位符，
            # 于是"拦一次重复查询"的代价是"执行一个垃圾查询"，得不偿失。
            # 现在只在【能给出一个真实的替代查询】时才改写，否则放行原查询。
            if selected and selected.lower().startswith("search["):
                q = selected[selected.find("[") + 1:selected.rfind("]")]
                if is_repeat_query(state, q):
                    alt = ""
                    for v in range(0, 3):
                        cand = build_query(task, variant=v)
                        if cand and not is_repeat_query(state, cand):
                            alt = cand
                            break
                    if alt:
                        print(f"🔁 R3 该查询已发过，改用规则拼的新查询: {alt[:60]}")
                        selected = f"search[{alt}]"
                    else:
                        print(f"🔁 R3 该查询已发过，但没有更好的替代 → 仍放行")

        # ---- 4b. 让位于真探（改动 F2）：只在结果页选商品这一步 ----
        # 这是本方法的贡献所在：结果页上模型只看得到标题和价格，看不到该商品有没有目标
        # 颜色/尺码，一旦点进去就被"当前已有部分分"锚住。所以"选哪个商品"最不该盲目直采。
        # 其余任何地方都不让位（那些地方信息就在眼前，让位只是把确定的决定随机化）。
        if (selected and use_probe and env.page_type == PAGE_RESULTS
                and re.match(r"^click\[b0[0-9a-z]{8}\]$", selected.strip(), re.I)):
            _a = selected.strip()[6:-1].lower()
            if ledger.has(_a):
                print(f"🎯 该候选已核实过（事实在备忘里），不让位，直接采纳: {selected}")
            else:
                print(f"🔬 意图直采让位于真探比较: {selected} → 走兜底横向比较")
                deferred_intent = selected
                selected = None

        # ---- 4b'【R4】没核实过本页任何候选，就不许离开结果页 ----
        # 实测病理（session_6 Step 2/3/4 连翻三页、session_0 Step 2 立刻翻页）：
        # 模型看结果页标题，发现没有 "acorn" / "b3-navy" / "30w x 34l"，就断定"这页没有"
        # 然后翻页。但**颜色和尺码从来不在标题里** —— 它们是商品页里的选项。
        # 判断一个候选行不行所需的信息客观地在商品页【里面】，所以"一个都没打开就断定这页
        # 没有"在逻辑上必然过早。第 1 页那个 Carhartt Relaxed Fit Jean 本来就是对的品类。
        #
        # 纯规则版没这个问题：它不做"这页有没有"的判断，直接对 top-K 真探、从事实里选，
        # 因此永远在第 1 页决策。这条规则就是把这个行为还给 Agent。
        # 真探完之后若确实全不对，翻页/重搜就是正当的 —— 那时本页已有账本记录，本条不触发。
        if (selected and use_probe and env.page_type == PAGE_RESULTS):
            _sl = selected.strip().lower()
            _leaving = (_sl in ("click[next >]", "click[next>]", "click[back to search]")
                        or _sl.startswith("search["))
            if _leaving:
                _unchecked = [p for p in env.products
                              if not ledger.has(p.asin) and p.asin not in skipped]
                if _unchecked:
                    print(f"🔬 R4 本页还有 {len(_unchecked)} 个未核实候选（颜色/尺码只在商品页"
                          f"里，标题看不出）→ 先真探再决定去留，不放行 {selected}")
                    deferred_intent = ""      # 不强制某个候选，让 quick_filter 自己排
                    selected = None

        # ---- 4c. 成交关口：buy now 之前逐条核对（有预算）----
        if selected and re.match(r"^click\[\s*buy\s*now\s*\]$", selected.strip(), re.I):
            live = env.item_page
            final = match_item(task, live)
            print(f"--- 买前核对 ---\n{final.summary()}")
            # 【诊断】记下这一刻的核对清单（可能被否决，最后一次留下的才算）
            diag["check_matched"] = "; ".join(final.matched)
            diag["check_missing"] = "; ".join(final.missing)
            diag["check_unknown"] = "; ".join(final.unknown)
            diag["check_soft"] = "; ".join(getattr(final, "matched_soft", []))
            diag["buy_asin"] = live.asin
            diag["buy_title"] = (live.title or "")[:120]
            diag["buy_price"] = live.price_low
            print(f"已选选项: {sorted(env.clicked_options_for(env.cur_asin)) or '（无）'}")
            unchecked = [p for p in env.products
                         if not ledger.has(p.asin) and p.asin not in skipped]
            ok, why, _c = commit_gate(
                task, live, final, {g: o for g, o in final.chosen_options.items()},
                unchecked, state.knowledge["skips_used"], steps_left,
                client, model, max_tokens, ledger=ledger)
            llm_calls += _c
            print(f"🚦 {why[:170]}")
            if not ok:
                state.knowledge["skips_used"] += 1
                skipped.add(env.cur_asin)
                # 【改动 B】否决后把该商品挂起，直到真探过一个新候选才解禁 ——
                # 否则 think 会看着备忘里的"已核实、仍可选"立刻把它点回来（实测循环）。
                suspended.add(env.cur_asin)
                selected = "click[< prev]"      # 回结果页看别的候选
                print(f"🚦 否决成交（第 {state.knowledge['skips_used']}/{MAX_SKIP} 次）→ 回结果页，并挂起 {env.cur_asin} 直到核实过新候选")

        took_intent = bool(selected)

        # ---- 5. 死锁重审（终版2.py 原样）----
        deadlock_reason = ""
        if not took_intent and not deferred_intent:
            if same_intent and (intent_repeat + 1) >= DEADLOCK_K:
                deadlock_reason = f"同一意图连续 {intent_repeat + 1} 步无法落地"
            elif optimistic_streak >= DEADLOCK_K:
                deadlock_reason = f"连续 {optimistic_streak} 步评估乐观但未完成"
        if deadlock_reason:
            print(f"🧭 死锁重审: {deadlock_reason}")
            rr, _c = re_review_deadlock(task, env, ledger, filtered, intent or last_intent,
                                        deadlock_reason, history, client, model, max_tokens)
            llm_calls += _c
            if rr:
                selected = rr
                print(f"🧭 重审改选: {selected}")

        if took_intent:
            print(f"🎯 意图直采: {selected}")

        # ---- 6. 兜底：quick_filter → step1 真探 → step2 → select ----
        if selected is None:
            if env.page_type == PAGE_SEARCH:
                # 【BUG1 修复】搜索页意图未落地 → 用 build_query 拼一个【真实】查询。
                # 绝不能落进 filtered[0]（早先那是占位符字面量，被真发出去过）。
                # 这也是 build_query 原本的用途：它此前定义了却从未被调用。
                for v in range(0, 3):
                    q = build_query(task, variant=v)
                    if q and not is_repeat_query(state, q):
                        selected = f"search[{q}]"
                        break
                if selected is None:
                    selected = f"search[{build_query(task, 0)}]"
                print(f"🅑 搜索页意图未落地 → 规则兜底查询: {selected}")
            elif env.page_type != PAGE_RESULTS:
                # 商品页/子页落不了地 → 退一层回结果页（不发非法动作）
                # 【退回顺序修复】必须【显式优先】click[< prev]（退回结果页，保住结果集），
                # 绝不优先 back to search（那会丢掉整个结果集、被迫重新搜索）。
                # 早先用 next(a for a in filtered if a in (...)) 按 filtered 顺序碰运气，
                # 而商品页 clickables 的顺序恰好是 ["back to search", "< prev", ...] ——
                # 永远先命中 back to search。实测 session_6 的 Step 10→37 就是这个循环
                # 重复七次、28 步全废。
                print(f"🅑 非结果页且意图未落地（intent={intent[:40]}）→ 尝试退回")
                _low = {a.lower(): a for a in filtered}
                selected = None
                for _pref in ("click[< prev]", "click[<prev]", "click[prev]"):
                    if _pref in _low:
                        selected = _low[_pref]
                        break
                if selected is None:
                    selected = _low.get("click[back to search]",
                                        filtered[0] if filtered else "click[back to search]")
                print(f"🅑 退回选择: {selected}")
            else:
                print(f"🅐 兜底：结果页横向比较（本页 {len(env.products)} 个商品）")
                cands, _c = quick_filter_candidates(
                    task, env.products, ledger, TOPK, client, model, max_tokens,
                    think_text, state.knowledge["filter_principles"])
                llm_calls += _c
                # 被让位的意图强制排在最前（终版2.py 改动F2 的配套：不能把模型的判断丢掉）
                if deferred_intent:
                    _a = deferred_intent.strip()[6:-1].lower()
                    cands = ([p for p in env.products if p.asin == _a]
                             + [p for p in cands if p.asin != _a])
                print(f"   待核实 {len(cands)} 个: {[p.asin for p in cands]}")

                for prod in cands:
                    page, res, verdict, _c = simulate_action(
                        task, env, prod.asin, prod, client, model, max_tokens,
                        use_probe=use_probe, use_step2=use_step2)
                    llm_calls += _c
                    if page is None:
                        print(f"   ⚠️ 真探失败，跳过 {prod.asin}")
                        continue
                    ledger.add(page, res, verdict)
                    suspended.clear()      # 【改动 B】看过新候选了，解禁被挂起的
                    print(f"   · {page.asin} ${page.price_low} {page.title[:40]!r}")
                    print(f"       命中={len(res.matched)} 缺失={res.missing or '无'}")
                    if verdict:
                        print(f"       step2: {verdict[:110]}")

                # 【BUG2 修复】只在"当前页真能点到"的候选里选
                clickable_now = {p.asin for p in env.products}
                asin, _c = select_best_action(task, ledger, skipped,
                                              client, model, max_tokens,
                                              clickable_now=clickable_now)
                llm_calls += _c
                if asin:
                    selected = f"click[{asin}]"
                    print(f"🤖 select 选定: {selected}")
                    _v = ledger.verdicts.get(asin, "")
                    is_optimistic = ("全中" in _v) or ("接近" in _v)
                else:
                    selected = next((a for a in filtered
                                     if a.lower() == "click[next >]"),
                                    filtered[0] if filtered else "click[back to search]")
                    print(f"⚠️ select 无可点候选，退化为: {selected}")
            deferred_intent = ""

        # ---- 7. 止损保证：步数将尽且账本里有候选 → 确定性成交 ----
        # 这一条保住零调用规则版的可靠性：不成交 = 0 分，部分匹配也有分。
        if (steps_left <= CLOSE_RESERVE and ledger.entries
                and env.page_type != PAGE_END):
            # 【BUG6 修复】止损此前也用 ledger.best() 全局挑，同样会挑到不在当前页的 ASIN
            # （抱枕那种），click 静默失败 → 止损机制跟着 BUG2/BUG3 一起失效了。
            # 现在优先在"当前页能点到的"里挑；挑不到就重发最后一次有效查询恢复现场。
            best = None
            if env.page_type == PAGE_ITEM and ledger.has(env.cur_asin) \
                    and env.cur_asin not in skipped:
                best = ledger.entries[env.cur_asin]     # 已经站在某个候选页上，就买它
            if best is None:
                here = {p.asin for p in env.products}
                best = (ledger.best(exclude=skipped, allow=here)
                        or ledger.best(allow=here))
            if best is None:
                qs = state.knowledge["queries_issued"]
                if qs and env.page_type != PAGE_RESULTS:
                    print(f"⏱ 剩余 {steps_left} 步，当前页无可点的已核实候选 → 重发查询恢复")
                    env.step(f"search[{qs[-1]}]" if env.page_type == PAGE_SEARCH
                             else "click[back to search]")
                    trajectory.append(StepRecord(kind="act", text="（止损：恢复现场）"))
                    here = {p.asin for p in env.products}
                    best = ledger.best(exclude=skipped, allow=here) or ledger.best(allow=here)
            if best is not None:
                bp, br = best
                print(f"⏱ 剩余 {steps_left} 步 → 强制成交最优候选 {bp.asin}")
                if env.cur_asin != bp.asin or env.page_type != PAGE_ITEM:
                    if env.page_type == PAGE_ITEM:
                        env.step("click[< prev]")
                        trajectory.append(StepRecord(kind="act", text="click[< prev]"))
                    if env.page_type == PAGE_RESULTS and \
                            bp.asin in {p.asin for p in env.products}:
                        env.step(f"click[{bp.asin}]")
                        trajectory.append(StepRecord(kind="act", text=f"click[{bp.asin}]"))
                if env.page_type == PAGE_ITEM:
                    env.ensure_options(task, trace=trajectory)   # 【改动 A】同一子程序
                    obs, reward, done = env.step("click[buy now]")
                    trajectory.append(StepRecord(kind="act", text="click[buy now]"))
                    print(f"观测: {obs[:160]}\n奖励: {reward}  完成: {done}")
                    if done:
                        _diag_capture(diag, env, task, obs, reward, "止损成交")
                        print(f"✓ 止损成交，得分 {reward}")
                        print(f"计入步数 {env.env_steps}  真探 {env.probe_steps}  "
                              f"LLM {llm_calls}")
                        return reward, True, trajectory, llm_calls, env.probe_steps

        # ---- 7b.【改动 A】买入前确定性补齐选项 ----
        # 所有买入路径都必须经过这里。原版只有止损路径会补选项（而它恰好是全程
        # 最可靠的成交方式），正常路径靠模型自己点、靠一本会失效的账本记账。
        if re.match(r"^click\[\s*buy\s*now\s*\]$", (selected or "").strip(), re.I):
            env.ensure_options(task, trace=trajectory)

        # ---- 8. 执行 ----
        print(f"选择动作: {selected}")
        trajectory.append(StepRecord(kind="act", text=selected))
        last_actions.append(selected)
        obs, reward, done = env.step(selected)
        trajectory.append(StepRecord(kind="ob", text=obs))
        print(f"观测: {obs[:220]}")
        print(f"奖励: {reward}, 完成: {done}")

        # ---- 9. 更新状态（终版2.py 原样，只是剪枝已在 env.step 里记账）----
        state.observations.append((selected, obs))
        state.knowledge["tried_actions"].add(selected)
        update_stuck_state(stuck_state, last_actions)

        if intent and same_intent and not took_intent:
            intent_repeat += 1
        else:
            intent_repeat = 0
        last_intent = intent
        optimistic_streak = optimistic_streak + 1 if (is_optimistic and not done) else 0
        if deadlock_reason:
            intent_repeat = optimistic_streak = 0

        history += f"\n> think: {think_text[:200]}\n> {selected}\n{obs[:400]}\n"
        if len(history) > 6400:      # 借鉴 ReAct 的 trace_prompt 截断
            history = history[-6400:]

        if done:
            _diag_capture(diag, env, task, obs, reward, "正常成交")
            print(f"✓ 完成，得分 {reward}")
            print(f"计入步数 {env.env_steps}  真探 {env.probe_steps}  LLM {llm_calls}")
            return reward, True, trajectory, llm_calls, env.probe_steps

    _diag_capture(diag, env, task, "", 0.0, "未成交/步数耗尽")
    print(f"未成交或步数耗尽。计入步数 {env.env_steps}  真探 {env.probe_steps}  LLM {llm_calls}")
    return 0.0, False, trajectory, llm_calls, env.probe_steps


# ================================================================
# 🔹【诊断】丢失项归因
# ================================================================
# 目标：回答"差 1 个 component 的那批任务，丢掉的是 color / size / price / attribute？"
#
# 依据：WebShop 的 reward ≈ (命中条目数) / (条目总数)，条目 = 每个要求的属性
# + 每个要求的选项 + 价格。环境回执里的 options 原文是"真的选中了什么"的唯一事实来源，
# 把它与任务 attrs 逐条比对即可定位。
# ⚠ 这里只做记录与统计，不参与任何决策。

DIAG_COLS = [
    ("session",        "Session"),
    ("reward",         "Reward"),
    ("n_total",        "分母N(反解)"),
    ("n_short",        "差几个"),
    ("path",           "成交路径"),
    ("miss_kind",      "疑似丢失项"),
    ("buy_asin",       "买入ASIN"),
    ("env_options",    "环境回执options(事实)"),
    ("task_attrs",     "任务要求attrs"),
    ("price_max",      "预算上限"),
    ("buy_price",      "买入标价low"),
    ("fill",           "ensure_options补点"),
    ("check_matched",  "买前MATCHED"),
    ("check_missing",  "买前MISSING"),
    ("check_unknown",  "买前UNKNOWN"),
    ("check_soft",     "买前仅标题命中"),
    ("buy_title",      "商品标题"),
    ("task_keywords",  "任务关键词"),
    ("steps",          "步数"),
    ("llm",            "LLM调用"),
    ("probes",         "真探步"),
]


def diag_infer_miss(d: Dict[str, Any]) -> str:
    """
    比对"任务要求的 attrs / 价格"与"环境回执里真的选中的 options"，
    列出哪些要求没有被落实。输出形如 "color未选中 | price超预算"。
    """
    out: List[str] = []
    env_opt = (d.get("env_options") or "").lower()
    # 环境回执形如 {"color": "dark heather", "size": "medium"}
    sel: Dict[str, str] = {}
    # 真实回执是 JSON 双引号（options [SEP] {"color": "dark heather", ...}）；
    # 单引号分支只为兼容自测里的 Python dict repr，不影响真实运行。
    for m in re.finditer(r"""['"]([^'"]+)['"]\s*:\s*['"]([^'"]*)['"]""", env_opt):
        sel[m.group(1).strip()] = m.group(2).strip()
    attrs = {}
    for part in (d.get("task_attrs") or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.strip().lower()] = v.strip().lower()
    for k, want in attrs.items():
        got = None
        for sk, sv in sel.items():
            if _norm(sk) == _norm(k) or _norm(k) in _norm(sk) or _norm(sk) in _norm(k):
                got = sv
                break
        if got is None:
            out.append(f"{k}未选中")
        elif not value_matches(want, [got])[0]:
            out.append(f"{k}选错({got})")
    if not sel and attrs:
        out.append("options为空(全部选项丢失)")
    pm, bp = d.get("price_max"), d.get("buy_price")
    if pm is not None and bp is not None and bp > pm + 1e-9:
        out.append("price超预算")
    return " | ".join(out) or "（attrs与选项都对上了，差的应是标题/描述类属性）"


def write_diagnosis(wb, rows: List[Dict[str, Any]]) -> None:
    """把诊断行写进第二张工作表，并在末尾附一张汇总。"""
    ws = wb.create_sheet("Diagnosis")
    for j, (_, label) in enumerate(DIAG_COLS, 1):
        ws.cell(row=1, column=j, value=label)
    for i, d in enumerate(rows, 2):
        d = dict(d)
        d["miss_kind"] = diag_infer_miss(d)
        for j, (key, _) in enumerate(DIAG_COLS, 1):
            v = d.get(key)
            ws.cell(row=i, column=j,
                    value=v if isinstance(v, (int, float)) or v is None else str(v)[:400])

    # ---- 汇总：差 1 个 component 的任务，丢的是什么 ----
    ws2 = wb.create_sheet("DiagSummary")
    ws2["A1"] = "差 N 个 component 的任务分布"
    ws2["A2"], ws2["B2"] = "差几个", "任务数"
    from collections import Counter
    cn = Counter(d.get("n_short") for d in rows if d.get("n_short") is not None)
    r = 3
    for k in sorted(x for x in cn if x is not None):
        ws2.cell(row=r, column=1, value=k)
        ws2.cell(row=r, column=2, value=cn[k])
        r += 1
    r += 1
    ws2.cell(row=r, column=1, value="疑似丢失项归因（全部任务）")
    r += 1
    ws2.cell(row=r, column=1, value="丢失项")
    ws2.cell(row=r, column=2, value="任务数")
    r += 1
    ck = Counter()
    for d in rows:
        for piece in diag_infer_miss(d).split(" | "):
            ck[piece] += 1
    for k, v in ck.most_common():
        ws2.cell(row=r, column=1, value=k)
        ws2.cell(row=r, column=2, value=v)
        r += 1
    r += 1
    ws2.cell(row=r, column=1, value="仅【差 1 个 component】的任务，丢失项归因")
    r += 1
    ws2.cell(row=r, column=1, value="丢失项")
    ws2.cell(row=r, column=2, value="任务数")
    r += 1
    c1 = Counter()
    for d in rows:
        if d.get("n_short") == 1:
            for piece in diag_infer_miss(d).split(" | "):
                c1[piece] += 1
    for k, v in c1.most_common():
        ws2.cell(row=r, column=1, value=k)
        ws2.cell(row=r, column=2, value=v)
        r += 1


# ================================================================
# 🔹 结果输出
# ================================================================

def write_results(scores: List[float], out_path: str, method: str,
                  llm_total: int, llm_per: List[int],
                  probe_total: int, probe_per: List[int],
                  steps_per: List[int], bought: List[bool],
                  diag_rows: Optional[List[Dict[str, Any]]] = None) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Results"
    n = len(scores)
    succ = sum(1 for s in scores if s >= 1.0)
    nb = sum(1 for b in bought if b)
    ws["A1"], ws["B1"] = "Method", method
    ws["A2"], ws["B2"] = "Total Episodes", n
    ws["A3"], ws["B3"] = "Avg Score", f"{sum(scores)/max(1,n):.4f}"
    ws["A4"], ws["B4"] = "Success Rate (==1.0)", f"{succ}/{n} = {succ/max(1,n):.2%}"
    ws["A5"], ws["B5"] = "Purchase Rate", f"{nb}/{max(1,n)} = {nb/max(1,n):.2%}"
    ws["A6"], ws["B6"] = "Total LLM Calls", llm_total
    ws["A7"], ws["B7"] = "Avg LLM Calls/Task", f"{llm_total/max(1,n):.2f}"
    ws["A8"], ws["B8"] = "Total Probe Steps (不占 max_steps)", probe_total
    ws["A9"], ws["B9"] = "Avg Steps/Task (计入)", \
        f"{sum(steps_per)/max(1,len(steps_per)):.1f}"
    ws["A11"], ws["B11"], ws["C11"], ws["D11"], ws["E11"] = \
        "Session", "Score", "LLM Calls", "Probe Steps", "Steps"
    for i, s in enumerate(scores):
        ws[f"A{12+i}"] = i
        ws[f"B{12+i}"] = round(s, 4)
        if i < len(llm_per):
            ws[f"C{12+i}"] = llm_per[i]
        if i < len(probe_per):
            ws[f"D{12+i}"] = probe_per[i]
        if i < len(steps_per):
            ws[f"E{12+i}"] = steps_per[i]
    if diag_rows:
        write_diagnosis(wb, diag_rows)
    wb.save(out_path)
    print(f"结果已保存到: {out_path}")


# ================================================================
# 🔹 主函数
# ================================================================

def main(mode: Optional[str] = None, out_path: Optional[str] = None):
    mode = (mode or os.getenv("WSPRO_MODE", "PRO")).strip().upper()
    if mode not in MODE_DESC:
        raise ValueError(f"mode 必须是 {'/'.join(MODE_DESC)} 之一，收到 {mode!r}")

    from web_agent_site.envs import WebAgentTextEnv



    # ---- 【固定题目】必须在构造 WebAgentTextEnv 之前设种子 ----
    # WebShop 有两处随机发生在它自己的 random.seed(233) 之前:
    #   engine.py:361  product_prices[asin] = random.uniform(*pricing[:2])  ← 商品成交价
    #   goal.py:39     _, price_upper = sorted(random.sample(price_range,2)) ← 题目预算上限
    # 于是每次进程启动约 2/3 的题预算都不同（实测 100 题里 69 题不一致），连带奖励里的
    # r_price 一起变 —— 这是此前"同代码两次运行 success 39 vs 35"方差的一个来源，
    # 也是连续五版改动测不出显著差异的原因之一。
    # ⚠ 三份实验代码与 goals 导出脚本必须用【同一个种子】，否则跑的不是同一批题。
    # WSPRO_SEED=off 则不设种子，退化为【随机题目】模式（与 2026-08-12 之前的
    # 九次历史运行同条件，可与它们比较）；默认 233 = 固定题目模式。
    _seed_env = os.getenv("WSPRO_SEED", "233").strip().lower()
    if _seed_env in ("off", "none", "random", "-1", ""):
        print("[随机题目] 未固定种子 —— 商品价格与题目预算每次运行都会变，"
              "仅用于与历史运行对比；新实验请用默认的固定种子模式")
    else:
        import random as _stdrandom
        _WS_SEED = int(_seed_env)
        _stdrandom.seed(_WS_SEED)
        try:
            import numpy as _np
            _np.random.seed(_WS_SEED)
        except Exception:
            pass
        print(f"[固定题目] random.seed({_WS_SEED}) 已在构造环境前设置 —— "
              f"商品价格与题目预算可复现")
    num_products = os.getenv("WEBSHOP_NUM_PRODUCTS")
    raw_env = WebAgentTextEnv(
        observation_mode="text",
        num_products=(int(num_products) if num_products else None))
    env = WebShopPro(raw_env)

    client = build_client()
    model = os.getenv("OPENAI_MODEL", "qwen3-max-2026-01-23")
    max_tokens = int(os.getenv("OPENAI_MAX_TOKENS", "4096"))
    num_episodes = int(os.getenv("WEBSHOP_NUM_EPISODES", "500"))
    max_steps = int(os.getenv("WEBSHOP_MAX_STEPS", "50"))

    print("=" * 66)
    print(f"[{VERSION}] {VERSION_DESC}")
    print(f"[{mode}] {MODE_DESC[mode]}")
    print(f"模型: {model} | sessions: {num_episodes} | max_steps: {max_steps} | topk: {TOPK}")
    print("架构 = 终版2.py 骨架（think -> 意图直采 -> 兜底 step1+step2 -> select），"
          "每层注入 WebShop 领域知识；决策由 LLM 做。")
    print("=" * 66)

    scores: List[float] = []
    llm_total = probe_total = 0
    llm_per: List[int] = []
    probe_per: List[int] = []
    steps_per: List[int] = []
    bought: List[bool] = []
    diag_rows: List[Dict[str, Any]] = []       # 【诊断】

    out_file = out_path or f"webshop_pro_results_{VERSION}_{mode}_{num_episodes}.xlsx"

    for idx in range(num_episodes):
        print(f"\n{'='*66}")
        print(f"[{mode}] 任务 {idx+1}/{num_episodes}: webshop/session_{idx}")
        print(f"{'='*66}")
        env.reset(session=idx)
        diag: Dict[str, Any] = {"session": idx}          # 【诊断】
        r, done, traj, calls, probes = run_episode(
            env, client=client, model=model, max_tokens=max_tokens,
            max_steps=max_steps, mode=mode, diag=diag)

        scores.append(float(r))
        llm_total += calls
        llm_per.append(calls)
        probe_total += probes
        probe_per.append(probes)
        steps_per.append(env.env_steps)
        bought.append(any(t.kind == "act" and "buy now" in t.text.lower() for t in traj))
        # 【诊断】补上运行时统计并打印归因（每个 episode 一行，肉眼也能看）
        diag["steps"], diag["llm"], diag["probes"] = env.env_steps, calls, probes
        diag_rows.append(diag)
        print(f"🔎 诊断: reward={diag.get('reward')} 差{diag.get('n_short')}个/"
              f"共{diag.get('n_total')}个 | 丢失项: {diag_infer_miss(diag)}")
        print(f"🔎 环境回执options: {diag.get('env_options') or '（空）'}")
        # 增量落盘：跑到一半中断也保得住已有数据
        try:
            write_results(scores, out_file,
                          method=f"[{VERSION}] {VERSION_DESC} | {MODE_DESC[mode]} [{mode}]",
                          llm_total=llm_total, llm_per=llm_per,
                          probe_total=probe_total, probe_per=probe_per,
                          steps_per=steps_per, bought=bought, diag_rows=diag_rows)
        except Exception as e:
            print(f"⚠ 增量落盘失败（不影响继续跑）: {str(e)[:80]}")

        succ = sum(1 for s in scores if s >= 1.0)
        nb = sum(1 for b in bought if b)
        print(f"\n当前进度: {idx+1}/{num_episodes}  [{mode}]")
        print(f"  score={r:.4f}  avg={sum(scores)/len(scores):.4f}  success={succ}/{len(scores)}")
        print(f"  平均步数={sum(steps_per)/len(steps_per):.1f}  成交率={nb}/{len(bought)}")
        print(f"  LLM 累计={llm_total}（平均 {llm_total/len(scores):.1f}/任务）"
              f"  真探累计={probe_total}")

    write_results(scores, out_file,
                  method=f"[{VERSION}] {VERSION_DESC} | {MODE_DESC[mode]} [{mode}]",
                  llm_total=llm_total, llm_per=llm_per,
                  probe_total=probe_total, probe_per=probe_per,
                  steps_per=steps_per, bought=bought, diag_rows=diag_rows)


if __name__ == "__main__":
    main()
