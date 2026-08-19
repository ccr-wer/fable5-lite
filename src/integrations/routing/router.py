# src/integrations/routing/router.py
"""轻量路由层（fable5-lite）。

- ``LightweightRouter``：根据任务关键词在 ``deepseek-v4-flash`` /
  ``deepseek-v4-pro`` 之间选择模型，纯本地实现、无外部依赖。
- ``Router``：轻量意图分类器（think / plan / act / validate），保留原
  ``fable_cycle`` / ``minimal_demo`` 的调用接口，纯本地实现、无外部依赖。
"""


# ── 模型路由：复杂度 → 模型选择 ──
class LightweightRouter:
    def __init__(self):
        self.flash = "deepseek-v4-flash"
        self.pro = "deepseek-v4-pro"
        self.complexity_keywords = {
            "high": ["设计", "架构", "跨会话", "长任务", "多步骤", "复杂"],
            "medium": ["分析", "总结", "生成", "处理"],
            "low": ["创建", "读取", "写入", "删除", "列出", "运行"]
        }

    def decide(self, task: str, stage: str = "think", complexity=None) -> str:
        """根据任务关键词选择模型。

        仅当任务命中 high 复杂度关键词时返回 ``self.pro``（deepseek-v4-pro），
        其余（含 medium / low 关键词或无匹配）一律返回 ``self.flash``（deepseek-v4-flash）。

        ``stage`` / ``complexity`` 为兼容 llm.py 调用签名而保留，不参与决策。
        """
        for level, keywords in self.complexity_keywords.items():
            for keyword in keywords:
                if keyword in task:
                    if level == "high":
                        return self.pro
        return self.flash  # 默认


def get_router():
    return LightweightRouter()


# ── 意图分类（保留 fable_cycle / minimal_demo 的调用接口）──
class Router:
    """轻量意图分类器：将任务归类到 think / plan / act / validate 之一。"""

    backend = "lightweight-router"

    def classify(self, task: str) -> str:
        t = (task or "").lower()
        if any(k in t for k in ["验证", "检查", "测试", "校验",
                                 "verify", "check", "test"]):
            return "validate"
        if any(k in t for k in ["设计", "规划", "架构", "方案",
                                 "design", "plan", "architect"]):
            return "plan"
        if any(k in t for k in ["执行", "运行", "创建", "写入", "删除", "做",
                                 "execute", "run", "create", "write"]):
            return "act"
        return "think"
