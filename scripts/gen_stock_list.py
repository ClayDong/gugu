"""全市场股票代码生成器（不依赖在线 API）。

生成沪深主板+创业板+科创板的股票代码列表，用于批量缓存。
基于 A 股代码规则：
- 沪市主板: 600000-603999 + 605000-605999
- 沪市科创板: 688000-688999
- 深市主板: 000001-000999 + 001000-001999
- 深市中小板: 002001-002999
- 深市创业板: 300001-300999

用法:
    from gen_stock_list import generate_all_codes
    codes = generate_all_codes()  # 返回 [(sina_symbol, code, ''), ...]
"""
from __future__ import annotations


def generate_all_codes() -> list[tuple[str, str, str]]:
    """生成全市场股票代码列表（不依赖在线 API）。

    返回 [(sina_symbol, code, name), ...]，name 为空（需要后续填充）。
    """
    codes: list[str] = []

    # 沪市主板 600000-603999
    for n in range(600000, 604000):
        codes.append(str(n))
    # 沪市主板 605000-605999
    for n in range(605000, 606000):
        codes.append(str(n))
    # 沪市科创板 688000-688999
    for n in range(688000, 689000):
        codes.append(str(n))
    # 深市主板 000001-001999
    for n in range(1, 2000):
        codes.append(f"{n:06d}")
    # 深市中小板 002001-002999
    for n in range(2001, 3000):
        codes.append(f"{n:06d}")
    # 深市创业板 300001-300999
    for n in range(300001, 301000):
        codes.append(str(n))

    # 转换为新浪格式
    result = []
    for code in codes:
        if code.startswith("6"):
            sina_sym = f"sh{code}"
        else:
            sina_sym = f"sz{code}"
        result.append((sina_sym, code, ""))

    return result


def main() -> None:
    """测试。"""
    codes = generate_all_codes()
    print(f"总代码数: {len(codes)}")
    # 按前缀统计
    from collections import Counter
    prefix_count = Counter(c[1][:3] for c in codes)
    for prefix, count in sorted(prefix_count.items()):
        print(f"  {prefix}xxx: {count} 只")


if __name__ == "__main__":
    main()
