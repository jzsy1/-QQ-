"""自修复测试目标文件 — 故意含 BUG。

calculate_sum 应返回 a + b，但当前实现写成 a - b。
R3 应能通过 read_code_file → write_code_file → reload_module 修复此 BUG。
"""


def calculate_sum(a: int, b: int) -> int:
    """计算两数之和。

    当前实现：return a - b  ← BUG（应为 a + b）
    期望：calculate_sum(3, 5) == 8
    """
    return a - b  # BUG: 应该是 a + b
