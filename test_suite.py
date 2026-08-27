# -*- coding: utf-8 -*-
"""
系统测试套件：推理引擎边界场景（多断点合并 / 合环转供 / 异常输入 / 状态隔离）
运行: python test_suite.py
"""
from inference_engine import PowerGraph, SWITCH_CLASSES

g = PowerGraph("ontology.yaml")
results = []
PASS = lambda name, cond, extra="": (results.append(cond), print(
    f"[{'PASS' if cond else 'FAIL'}] {name}" + (f"  → {extra}" if extra and not cond else "")))


print("=" * 64)
print("T1  初始状态（全开关闭合，L1 常开）应全网正常")
print("-" * 64)
outage = g.compute_outage()
PASS("T1 无失电设备", len(outage) == 0, f"实际失电: {sorted(outage)}")
PASS("T1 L1 保持常开", not g.is_closed("L1"))

print()
print("=" * 64)
print("T2  多断点合并（规则 R9）：S1 断开 + S3 断开")
print("-" * 64)
# S1下游1000户 + S3下游(T4/T5)700户 = 1700
r = g.analyze_outage(["S1", "S3"])
PASS("T2 失电配变去重合并", r["outage_transformers"] == ["T1", "T2", "T3", "T4", "T5"],
     f"实际: {r['outage_transformers']}")
PASS("T2 停电户数 = 1700", r["customer_count"] == 1700, f"实际: {r['customer_count']}")

print()
print("=" * 64)
print("T3  合环转供：S2 断开 + 合上 L1 → 后援供电恢复（无失电）")
print("-" * 64)
# 模拟: S2 断开, L1 合闸
g.devices["S2"]["isClosed"] = False
g.devices["L1"]["isClosed"] = True
outage = g.compute_outage()
PASS("T3 合环后无失电设备", len(outage) == 0, f"实际失电: {sorted(outage)}")
# 恢复
g.devices["S2"]["isClosed"] = True
g.devices["L1"]["isClosed"] = False

print()
print("=" * 64)
print("T4  L1 单独合闸（常开→合闸）不产生失电")
print("-" * 64)
g.devices["L1"]["isClosed"] = True
PASS("T4 无失电设备", len(g.compute_outage()) == 0)
g.devices["L1"]["isClosed"] = False

print()
print("=" * 64)
print("T5  状态隔离：analyze_outage 临时断点不改变持久开关状态")
print("-" * 64)
g.analyze_outage(["S1"])          # 临时计算
PASS("T5 S1 仍为闭合", g.is_closed("S1"))

print()
print("=" * 64)
print("T6  负荷转移边界：T1 过载可转移 / 空区域 / 未知联络开关")
print("-" * 64)
tf = g.transfer_feasibility(["T1"])
PASS("T6 T1 过载可经 L1 转移", tf["feasible"] and tf["load_rate_after"] == 0.8187,
     f"实际: {tf}")
tf2 = g.transfer_feasibility(["CB1"])          # 区域无配变(断路器不是配变)
PASS("T6 无配变区域 → 拒绝", not tf2["feasible"] and "无配变" in tf2["reason"], f"实际: {tf2}")
tf3 = g.transfer_feasibility(["T1"], tie_id="LX") # 不存在的联络开关
PASS("T6 未知联络开关 → 拒绝", not tf3["feasible"] and "不存在" in tf3["reason"], f"实际: {tf3}")

print()
print("=" * 64)
print("T7  检修隔离边界：未知工单 / 已知工单")
print("-" * 64)
iso = g.isolation_for_work_order("WO-999")
PASS("T7 未知工单返回错误", "error" in iso, f"实际: {iso}")
iso2 = g.isolation_for_work_order("WO-001")
PASS("T7 WO-001 隔离 S2 + 防倒送 L1",
     iso2["isolate_switches"] == ["S2"] and iso2["keep_open"] == ["L1"])

print()
print("=" * 64)
print("T8  过载巡检：T1 过载 88.9%，其余正常")
print("-" * 64)
overs = g.check_overloads()
PASS("T8 仅 T1 过载", [o["device"] for o in overs] == ["T1"], f"实际: {overs}")

print()
print("=" * 64)
print("T9  全开关状态一致性（断路器/分段开关初始全部闭合）")
print("-" * 64)
sws = [sid for sid, d in g.devices.items() if d.get("cls") in ("Breaker", "Sectionalizer")]
PASS("T9 分段/断路器全部闭合", all(g.is_closed(s) for s in sws), f"实际: {sws}")

print()
passed = sum(1 for ok in results if ok)
total = len(results)
print("=" * 64)
print(f"测试结果: {passed}/{total} 通过")
print("ALL TESTS PASSED ✔" if all(results) else "存在失败用例!")
exit(0 if all(results) else 1)
