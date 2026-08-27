# -*- coding: utf-8 -*-
"""
配电网本体推理引擎 (Inference Engine)
=====================================
基于 ontology.yaml 中声明的类/属性/规则，实现确定性的符号推理：
  R1 故障传播   - 断开的开关 -> 下游设备失电
  R2 停电影响   - 配变失电 -> 其服务的用户停电
  R3 影响户数   - 失电用户数聚合
  R4/R5 过载触发 - 配变 loadRate>0.8 / 馈线 loadRate>0.9
  R6 转移可行性 - 失电/过载区域经联络开关转供对侧馈线的判定
  R7 转移后校验 - 合闸后对侧是否过载
  R8 检修隔离   - 工单覆盖设备的上游开关隔离 + 防倒送电
  R9 多断点合并 - 失电集合去重

零第三方依赖（仅 PyYAML 读取本体文件）。
"""
from __future__ import annotations
import yaml
from typing import Dict, List, Set, Optional, Any

# ---------------- 配置阈值（与 ontology.yaml 规则 R4/R5 对应） ----------------
T_OVERLOAD_RATE = 0.8      # 配变过载阈值
F_OVERLOAD_RATE = 0.9      # 馈线过载阈值
SAFE_CAP_RATE = 0.9        # 转移判定时对侧馈线的安全运行容量系数

# 开关类型（可闭合/可断开）
SWITCH_CLASSES = {"Switch", "Breaker", "Sectionalizer", "TieSwitch"}


class PowerGraph:
    """从 ontology.yaml 实例数据构建的可推理电力拓扑图"""

    def __init__(self, ontology_path: str):
        with open(ontology_path, "r", encoding="utf-8") as f:
            self.onto = yaml.safe_load(f)
        self.instances = self.onto["instances"]
        self._build_index()

    # ---------------------------------------------------------- 构建索引
    def _build_index(self):
        self.devices: Dict[str, dict] = {}          # 所有导电设备/电源
        self.customers: Dict[str, dict] = {}
        self.work_orders: Dict[str, dict] = {}
        self.feeders: Dict[str, dict] = {}
        self.substations: Dict[str, dict] = {}
        self.busbars: Dict[str, dict] = {}
        self.tie_switches: Dict[str, dict] = {}

        for s in self.instances["switches"]:
            self.devices[s["id"]] = {**s, "type": "switch"}
        for t in self.instances["transformers"]:
            self.devices[t["id"]] = {**t, "type": "transformer"}
        for l in self.instances["lineSegments"]:
            self.devices[l["id"]] = {**l, "type": "linesegment"}
        for b in self.instances["busbars"]:
            self.busbars[b["id"]] = b
        for f in self.instances["feeders"]:
            self.feeders[f["id"]] = f
        for c in self.instances["customers"]:
            self.customers[c["id"]] = c
        for w in self.instances["workOrders"]:
            self.work_orders[w["id"]] = w
        for s in self.instances["substations"]:
            self.substations[s["id"]] = s
        for t in self.devices.values():
            if t.get("cls") == "TieSwitch":
                self.tie_switches[t["id"]] = t

        # 计算负载率（数据属性派生）
        for d in self.devices.values():
            rc = d.get("ratedCapacity")
            cl = d.get("currentLoad")
            d["loadRate"] = round(cl / rc, 4) if rc else 0.0

    # ---------------------------------------------------------- 基础访问
    def is_closed(self, dev_id: str) -> bool:
        """开关是否合闸；非开关设备视为导通"""
        d = self.devices.get(dev_id)
        if not d:
            return False
        if d.get("cls") in SWITCH_CLASSES:
            return bool(d.get("isClosed", False))
        return True

    def downstream_of(self, dev_id: str) -> List[str]:
        return list(self.devices.get(dev_id, {}).get("downstream", []) or [])

    def upstream_of(self, dev_id: str) -> List[str]:
        return list(self.devices.get(dev_id, {}).get("upstream", []) or [])

    def feeder_of(self, dev_id: str):
        return self.devices.get(dev_id, {}).get("feeder_of")

    # ============================================================
    # 规则 R1+R9：故障传播 / 多断点合并
    # 正确语义: 失电设备 = 全部设备 - 带电设备 - 断开的开关
    # (带电设备从母线出发经闭合开关 BFS, 含闭合联络开关的后援供电)
    # ============================================================
    def _neighbors(self, dev_id: str) -> List[str]:
        """
        拓扑邻居:
          - 下游设备 (母线实例的 downstream 提供电源出口)
          - 闭合联络开关: 端点 -> L1 -> 另一端点 (L1 作为中间节点参与传导)
        """
        nbs = list(self.downstream_of(dev_id))
        if dev_id in self.busbars:
            nbs += list(self.busbars[dev_id].get("downstream", []) or [])
        for tid, tie in self.tie_switches.items():
            if not tie.get("isClosed", False):
                continue
            a, b = tie.get("endA"), tie.get("endB")
            if dev_id == tid:                 # 联络开关自身: 连接两个端点
                if a:
                    nbs.append(a)
                if b:
                    nbs.append(b)
            elif dev_id == a:                 # 端点A: 经 L1 传导到 L1
                nbs.append(tid)
            elif dev_id == b:
                nbs.append(tid)
        return nbs

    def _energized(self) -> Set[str]:
        """从所有母线(电源)出发, 沿闭合开关可达的带电设备集合"""
        energized: Set[str] = set()
        for bid in self.busbars:
            queue, seen = [bid], {bid}
            while queue:
                cur = queue.pop(0)
                energized.add(cur)
                for nxt in self._neighbors(cur):
                    if nxt in seen:
                        continue
                    d = self.devices.get(nxt, {})
                    if d.get("cls") in SWITCH_CLASSES and not d.get("isClosed", False):
                        continue          # 断开的开关不可跨越
                    seen.add(nxt)
                    queue.append(nxt)
        return energized

    def compute_outage(self, open_switches: List[str] = None) -> Set[str]:
        """
        计算当前失电设备集合。
        open_switches 非空时, 临时将这些开关置为断开后计算(不改变持久状态)。
        """
        open_set = set(open_switches or [])
        restore = {}
        for sw in open_set:
            d = self.devices.get(sw)
            if d and d.get("cls") in SWITCH_CLASSES:
                restore[sw] = d.get("isClosed", False)
                d["isClosed"] = False
        try:
            energized = self._energized()
        finally:
            for sw, val in restore.items():
                self.devices[sw]["isClosed"] = val

        outage: Set[str] = set()
        for did, d in self.devices.items():
            if d.get("cls") in SWITCH_CLASSES and (not self.is_closed(did) or did in open_set):
                continue                # 断开的开关本身不算失电设备
            if did not in energized:
                outage.add(did)
        return outage

    # ============================================================
    # 规则 R2+R3：停电影响用户 / 影响户数聚合
    # ============================================================
    def affected_customers(self, outage_devices: Set[str]) -> dict:
        """输入失电设备集合，返回 {transformers, customer_groups, count}"""
        tfs = sorted(
            d for d in outage_devices if self.devices[d]["type"] == "transformer"
        )
        groups, count = [], 0
        for t in tfs:
            for cid in self.devices[t].get("serves", []) or []:
                groups.append(cid)
                count += self.customers[cid].get("customerCount", 0)
        return {"transformers": tfs, "customer_groups": groups,
                "customer_count": count}

    # ============================================================
    # 规则 R4+R5：过载触发
    # ============================================================
    def check_overloads(self, dev_ids: Optional[List[str]] = None) -> List[dict]:
        """返回过载设备告警列表"""
        result = []
        scope = dev_ids or list(self.devices.keys())
        for did in scope:
            d = self.devices[did]
            rate = d.get("loadRate", 0.0)
            if d["type"] == "transformer" and rate > T_OVERLOAD_RATE:
                result.append({"device": did, "loadRate": rate,
                               "level": "配变过载", "threshold": T_OVERLOAD_RATE})
        for fid, f in self.feeders.items():
            rate = f.get("currentLoad", 0) / f["ratedCapacity"] if f["ratedCapacity"] else 0
            if rate > F_OVERLOAD_RATE:
                result.append({"device": fid, "loadRate": round(rate, 4),
                               "level": "馈线过载", "threshold": F_OVERLOAD_RATE})
        return result

    # ============================================================
    # 规则 R6+R7：负荷转移可行性判定 + 转移后容量校验
    # ============================================================
    def _expand_region(self, region: Set[str]) -> Set[str]:
        """沿 upstream 扩展区域直到开关/联络开关端点，使区域能命中联络开关"""
        region = set(region)
        queue = list(region)
        while queue:
            cur = queue.pop(0)
            for up in self.upstream_of(cur):
                if up in region or up in self.busbars:
                    continue          # 母线是电源点，不作为转移区域
                d = self.devices.get(up, {})
                if d.get("cls") in SWITCH_CLASSES:
                    continue          # 开关是隔离点，不进入
                region.add(up)
                queue.append(up)
        return region

    def transfer_feasibility(self, region_devices: List[str],
                             tie_id: str = "L1") -> dict:
        """
        判定失电/过载区域能否经联络开关转移到对侧馈线。
        region_devices: 失电或过载的设备集合（配变/线路段均可）
        """
        tie = self.tie_switches.get(tie_id)
        if not tie:
            return {"feasible": False, "reason": f"联络开关 {tie_id} 不存在"}

        region = self._expand_region(set(region_devices))

        # 区域转移负荷 = 区域内配变当前负荷之和（R6 前提，最先短路）
        transfer_load = sum(
            self.devices[d].get("currentLoad", 0)
            for d in region if self.devices.get(d, {}).get("type") == "transformer"
        )
        if transfer_load <= 0:
            return {"feasible": False, "reason": "该区域无配变负荷可转移"}

        end_a, end_b = tie.get("endA"), tie.get("endB")
        # 对侧设备 = 联络开关另一端不在区域内的一端
        if end_a in region:
            other = end_b
        elif end_b in region:
            other = end_a
        else:
            return {"feasible": False,
                    "reason": f"{tie_id} 未接入该区域，无法用于转供"}

        other_feeder = self.feeder_of(other)
        if not other_feeder:
            return {"feasible": False, "reason": f"对侧设备 {other} 不属于任何馈线"}

        # 对侧馈线安全剩余容量（R6 判定）
        f2 = self.feeders[other_feeder]
        safe_cap = f2["ratedCapacity"] * SAFE_CAP_RATE
        cur_load = f2.get("currentLoad", 0)
        remaining = safe_cap - cur_load
        load_after = cur_load + transfer_load
        load_rate_after = round(load_after / f2["ratedCapacity"], 4)

        feasible = transfer_load <= remaining
        if feasible:
            reason = (f"可经 {tie_id} 转移 {transfer_load}kVA 至 {other_feeder}："
                      f"合闸后 {other_feeder} 负载率 {load_rate_after:.1%}，"
                      f"未超过 {F_OVERLOAD_RATE:.0%} 安全阈值")
        else:
            reason = (f"拒绝转移：区域负荷 {transfer_load}kVA 超过 {other_feeder} "
                      f"剩余安全容量 {remaining:.0f}kVA，合闸后负载率 "
                      f"{load_rate_after:.1%} 将超过 {F_OVERLOAD_RATE:.0%}")
        return {"feasible": feasible, "reason": reason,
                "transfer_load": transfer_load, "target_feeder": other_feeder,
                "remaining_capacity": round(remaining, 1),
                "load_rate_after": load_rate_after,
                "action": f"合上 {tie_id}" if feasible else f"保持 {tie_id} 断开"}

    # ============================================================
    # 规则 R8：检修隔离（上游开关隔离 + 防倒送电）
    # ============================================================
    def isolation_for_work_order(self, wo_id: str) -> dict:
        wo = self.work_orders.get(wo_id)
        if not wo:
            return {"work_order": wo_id, "error": "工单不存在"}
        covered = wo.get("coveredEquipment", []) or []
        to_open, keep_open, reasons = [], [], []
        for eid in covered:
            # 沿 upstream 找最近的开关（R8）
            cur = eid
            seen = set()
            while cur not in seen:
                seen.add(cur)
                d = self.devices.get(cur)
                if not d:
                    break
                if d.get("cls") in SWITCH_CLASSES:
                    to_open.append(cur)
                    reasons.append(f"断开 {cur}：隔离 {eid} 的电源")
                    break
                ups = self.upstream_of(cur)
                if not ups:
                    reasons.append(f"{eid} 上游无开关可隔离（检查拓扑）")
                    break
                cur = ups[0]
            # 防倒送电：设备可达的联络开关保持断开（R8）
            for tie_id, tie in self.tie_switches.items():
                ends = [tie.get("endA"), tie.get("endB")]
                if eid in ends or any(
                    self._reachable_without_tie(eid, end) for end in ends
                ):
                    keep_open.append(tie_id)
                    reasons.append(f"保持 {tie_id} 断开：防止对侧馈线倒送电")
        return {"work_order": wo_id, "isolate_switches": sorted(set(to_open)),
                "keep_open": sorted(set(keep_open)), "reasons": reasons}

    def _reachable_without_tie(self, start: str, target: str) -> bool:
        """不跨联络开关的前提下，start 与 target 是否连通（双向 BFS）"""
        if start == target:
            return True
        queue, seen = [start], {start}
        while queue:
            cur = queue.pop(0)
            for nxt in self.upstream_of(cur) + self.downstream_of(cur):
                d = self.devices.get(nxt, {})
                if d.get("cls") == "TieSwitch" or nxt in seen:
                    continue
                seen.add(nxt)
                if nxt == target:
                    return True
                queue.append(nxt)
        return False

    # ============================================================
    # 综合场景：失电 + 影响户数 + 转供建议（供 Agent 使用）
    # ============================================================
    def analyze_outage(self, open_switches: List[str]) -> dict:
        """断点分析：失电设备 -> 失电配变/用户 -> 转供建议"""
        outage = self.compute_outage(open_switches)
        aff = self.affected_customers(outage)
        # 转供建议：以失电配变为区域，引擎自动扩展至联络开关接入点（R6）
        tie_hint = ""
        if self.tie_switches:
            tf = self.transfer_feasibility(aff["transformers"])
            tie_hint = tf["reason"]
        return {
            "open_switches": open_switches,
            "outage_devices": sorted(outage),
            "outage_transformers": aff["transformers"],
            "outage_customer_groups": aff["customer_groups"],
            "customer_count": aff["customer_count"],
            "transfer_hint": tie_hint,
        }


if __name__ == "__main__":
    g = PowerGraph("ontology.yaml")
    print("== 负载率 ==")
    for d in g.devices.values():
        if d["type"] == "transformer":
            print(f"  {d['id']}: {d['loadRate']:.1%}")
    print("\n== 过载告警 ==", g.check_overloads())
