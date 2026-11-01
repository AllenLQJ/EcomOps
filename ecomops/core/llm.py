"""本项目无真实 LLM：各模块用确定性规则实现。此处只提供故障注入，便于验证 fallback_used 降级路径。
用法：`if llm.should_fail("evidence_track"): fallback_used = True; ...`。
故障清单来自环境变量 ECOMOPS_LLM_FAIL（逗号分隔，* 表示全部）或 /internal/v1/faults 注入。"""
import os

_injected: set[str] = set()


def set_failures(tasks: list[str]) -> None:
    _injected.clear()
    _injected.update(tasks)


def should_fail(task: str) -> bool:
    env = {t.strip() for t in os.environ.get("ECOMOPS_LLM_FAIL", "").split(",") if t.strip()}
    return bool({task, "*"} & (env | _injected))
