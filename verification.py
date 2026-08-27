# -*- coding: utf-8 -*-
"""
推理验证脚本：5 个推理问题 + 预期答案 + 断言
运行: python verification.py
全部通过输出 "ALL 5 QUESTIONS PASSED"
"""
from inference_engine import PowerGraph

g = PowerGraph("ontology.yaml")
results = []


def check(name: str, actual: object, expected: object, detail: str = ""):
    ok = actual == expected
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       预期: {expected}")
        print(f"       实际: {actual}")
    if detail:
        print(f"       {detail}")


def check_approx(name: str, actual: float, expected: float, tol: float = 0.001):
    """浮点容差断言(避免 round 精度差异)"""
    ok = abs(actual - expected) <= tol
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}")
    if not ok:
        print(f"       预期: {expected} (±{tol})")
        print(f"       实际: {actual}")


print("=" * 70)
print("Q1: 分段开关 S1 断开后，哪些配变失电？影响多少用户？")
print("-" * 70)
r = g.analyze_outage(["S1"])
check("Q1 失电配变", r["outage_transformers"], ["T1", "T2", "T3"])
check("Q1 停电用户数", r["customer_count"], 1000)
print(f"       推理链路: S1断开 → 下游[{','.join(r['outage_devices'])}] 失电 → 用户 {r['customer_count']} 户")
print(f"       转供建议: {r['transfer_hint']}")

print()
print("=" * 70)
print("Q2: 配变 T1 负载率 88.9%（过载），能否经联络开关 L1 转移到馈线 F2？")
print("-" * 70)
overloads = g.check_overloads(["T1"])
check("Q2 T1 过载告警", [o["device"] for o in overloads], ["T1"])
tf = g.transfer_feasibility(["T1"])
check("Q2 转移可行性", tf["feasible"], True)
check_approx("Q2 转移后F2负载率", tf["load_rate_after"], 0.819)
print(f"       {tf['reason']}")

print()
print("=" * 70)
print("Q3: 馈线 F1 首端断路器 CB1 跳闸，影响多少用户？")
print("-" * 70)
r = g.analyze_outage(["CB1"])
check("Q3 失电配变", r["outage_transformers"], ["T0", "T1", "T2", "T3"])
check("Q3 停电用户数", r["customer_count"], 1100)
print(f"       推理链路: CB1断开 → F1 全线失电(含T0) → 用户 {r['customer_count']} 户")

print()
print("=" * 70)
print("Q4: 检修工单 WO-001（检修配变 T1），需要断开哪些开关？")
print("-" * 70)
iso = g.isolation_for_work_order("WO-001")
check("Q4 隔离开关", iso["isolate_switches"], ["S2"])
check("Q4 防倒送保持断开", iso["keep_open"], ["L1"])
for reason in iso["reasons"]:
    print(f"       {reason}")

print()
print("=" * 70)
print("Q5: 分段开关 S2 断开（T1/T2 失电 860kVA），合上 L1 能否恢复供电？")
print("-" * 70)
r = g.analyze_outage(["S2"])
check("Q5 失电配变", r["outage_transformers"], ["T1", "T2"])
check("Q5 停电用户数", r["customer_count"], 800)
tf = g.transfer_feasibility(["LsegX", "T1", "T2"])
check("Q5 转移可行性(应拒绝)", tf["feasible"], False)
check_approx("Q5 合闸后F2负载率", tf["load_rate_after"], 1.006)
print(f"       {tf['reason']}")

print()
print("=" * 70)
print("附加: 全图过载巡检（规则 R4/R5 扫描）")
print("-" * 70)
print(f"       {g.check_overloads()}")

print()
passed = sum(results)
print("=" * 70)
print(f"验证结果: {passed}/{len(results)} 通过")
if all(results):
    print("ALL 5 QUESTIONS PASSED ✔ 本体 + 规则可完整支撑推理性问答")
else:
    print("存在失败断言，请检查!")
exit(0 if all(results) else 1)
