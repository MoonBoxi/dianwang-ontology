# -*- coding: utf-8 -*-
"""
LangGraph 对话编排层 (Agent)
=============================
用 LangGraph StateGraph 将"意图解析 → 条件路由 → 领域推理 → 答案组装"编排为
可执行图, 每个推理动作是一个节点(工具), 确定性规则引擎负责计算,
LLM(可选) 只参与意图解析 —— 符号推理 + LLM 混合架构。

图结构:
  parse(意图解析) --[route]--> outage / transfer / overload / isolation
                              / status / fallback --> END

对外接口: run_agent(question, devs) -> {intent, answer, trace, data}
"""
from __future__ import annotations
import json
import os
import re
from typing import Dict, List, TypedDict

from langgraph.graph import StateGraph, END

from inference_engine import PowerGraph, SWITCH_CLASSES

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ONTOLOGY_PATH = os.path.join(BASE_DIR, "ontology.yaml")

# ---------------------------------------------------------------- 共享实例
graph = PowerGraph(ONTOLOGY_PATH)

SWITCH_IDS = [sid for sid, d in graph.devices.items()
              if d.get("cls") in ("Breaker", "Sectionalizer", "TieSwitch")]
TIE_IDS = [tid for tid, d in graph.devices.items() if d.get("cls") == "TieSwitch"]
XFMR_IDS = [tid for tid, d in graph.devices.items() if d["type"] == "transformer"]


# ---------------------------------------------------------------- Agent 状态
class AgentState(TypedDict):
    question: str
    devs: List[str]          # 问题中提到的设备 id
    intent: str
    answer: str
    trace: List[str]
    data: dict


def _mentioned(q: str) -> List[str]:
    """提取问题中提到的设备 id"""
    return [d for d in (SWITCH_IDS + XFMR_IDS) if d in q.upper()]


def fmt_device(did: str) -> str:
    return graph.devices.get(did, {}).get("name", did)


# ---------------------------------------------------------------- LLM 可选解析
def llm_parse(question: str):
    """OpenAI 兼容接口解析意图(可选)。失败返回 None -> 降级规则解析"""
    key = os.environ.get("LLM_API_KEY")
    if not key:
        return None
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    try:
        import requests
        sys_prompt = (
            "你是配电网问答意图解析器。从问题中提取 JSON："
            '{"intent": "outage|transfer|overload|isolation|status", '
            '"devices": ["S1"], "tie": "L1"}。devices 只填问题中出现的设备id'
            "(S1/S2/S3/CB1/CB2/T0~T5/L1)。只输出 JSON。")
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "messages": [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": question}],
                "temperature": 0, "response_format": {"type": "json_object"}},
            timeout=8)
        data = resp.json()["choices"][0]["message"]["content"]
        return json.loads(data)
    except Exception:
        return None


# ---------------------------------------------------------------- 快捷指令映射
QUICK_QUESTIONS = {
    "1": "S1 断开后影响哪些用户？",
    "2": "配变 T1 过载怎么办？",
    "3": "检修配变 T1 需要断开哪些开关？",
    "4": "当前电网运行状态？",
    "5": "S2 断开后合上 L1 能否恢复供电？",
}


# ---------------------------------------------------------------- 节点 1: 意图解析
def parse_node(state: AgentState) -> AgentState:
    # 数字快捷指令: 输入 "2" 等价于问 "配变 T1 过载怎么办？"
    raw = state["question"].strip()
    if raw in QUICK_QUESTIONS:
        state = {**state, "question": QUICK_QUESTIONS[raw]}
    q = state["question"].upper()
    devs = _mentioned(state["question"])

    # 可选 LLM 解析(优先), 失败/未配置降级规则
    parsed = llm_parse(state["question"])
    if parsed and parsed.get("intent") != "other":
        devs = devs or parsed.get("devices", [])
        intent = parsed.get("intent", "other")
    else:
        if any(k in q for k in ["检修", "工单", "隔离"]):
            intent = "isolation"
        elif any(k in q for k in ["转移", "转供", "切换", "备用"]):
            intent = "transfer"
        elif any(k in q for k in ["过载", "超载"]):
            intent = "overload"
        elif any(k in q for k in ["断开", "跳闸", "失电", "停电", "影响"]):
            intent = "outage"
        elif any(k in q for k in ["状态", "拓扑", "结构", "情况", "运行"]):
            intent = "status"
        else:
            intent = "other"
    return {**state, "intent": intent, "devs": devs}


# ---------------------------------------------------------------- 业务节点(确定性推理)
def outage_node(state: AgentState) -> AgentState:
    devs = state["devs"]
    opens = [d for d in devs if d in SWITCH_IDS and d not in TIE_IDS]
    if not opens:
        # 兜底: 默认分析第一个断点 S1 (保持演示友好)
        opens = ["S1"]
    r = graph.analyze_outage(opens)
    trace = [
        f"[R1 故障传播] 断点 {opens} → 下游失电设备 {len(r['outage_devices'])} 个: "
        f"{', '.join(r['outage_devices'])}",
        f"[R2 停电影响] 失电配变: {', '.join(r['outage_transformers'])}",
        f"[R3 影响户数] {', '.join(r['outage_customer_groups'])} 用户组, 共 {r['customer_count']} 户",
    ]
    if r["transfer_hint"]:
        trace.append(f"[R6/R7 转供建议] {r['transfer_hint']}")
    answer = (f"『{fmt_device(opens[0])}』断开后：\n"
              f"· 失电设备 {len(r['outage_devices'])} 个（{', '.join(r['outage_devices'])}）\n"
              f"· 失电配变：{', '.join(r['outage_transformers'])}\n"
              f"· 停电用户：{r['customer_count']} 户"
              + (f"\n· 转供建议：{r['transfer_hint']}" if r["transfer_hint"] else ""))
    return {**state, "answer": answer, "trace": trace, "data": r}


def transfer_node(state: AgentState) -> AgentState:
    q = state["question"].upper()
    if any(t in q for t in XFMR_IDS):
        region = [t for t in XFMR_IDS if t in q]
    else:
        region = [t for t in XFMR_IDS if graph.devices[t].get("loadRate", 0) > 0.8]
    tf = graph.transfer_feasibility(region)
    trace = [f"[R6 转移可行性] 区域配变 {region} 经联络开关 {tf.get('action', 'L1')}",
             f"   -> 转移负荷 {tf.get('transfer_load', 0)}kVA",
             f"   -> 目标馈线 {tf.get('target_feeder')} 合闸后负载率 {tf.get('load_rate_after', 0):.1%}",
             f"[R7 容量校验] {'未超阈值, 允许合闸' if tf['feasible'] else '超过90%阈值, 拒绝合闸'}"]
    return {**state, "answer": tf["reason"], "trace": trace, "data": tf}


def overload_node(state: AgentState) -> AgentState:
    q = state["question"].upper()
    scope = [t for t in XFMR_IDS if t in q] or None
    overs = graph.check_overloads(scope)
    trace = ["[R4/R5 过载触发] 负载率阈值 配变>0.8 馈线>0.9"]
    if overs:
        for o in overs:
            trace.append(f"   -> {fmt_device(o['device'])} 负载率 {o['loadRate']:.1%} [{o['level']}]")
        answer = "过载告警：\n" + "\n".join(
            f"  {fmt_device(o['device'])} 负载率 {o['loadRate']:.1%}（{o['level']}）"
            for o in overs)
        for o in overs:
            if o["device"] in XFMR_IDS:
                tf = graph.transfer_feasibility([o["device"]])
                trace.append(f"[R6] {o['device']} 转供建议: {tf['reason']}")
                answer += f"\n  💡 转供建议：{tf['reason']}"
    else:
        answer = "当前无设备过载。"
    return {**state, "answer": answer, "trace": trace, "data": {"overloads": overs}}


def isolation_node(state: AgentState) -> AgentState:
    wo_id = "WO-001"
    iso = graph.isolation_for_work_order(wo_id)
    trace = [f"[R8 检修隔离] 工单 {wo_id} 覆盖设备 {iso['work_order']}"]
    trace.extend(f"   -> {r}" for r in iso["reasons"])
    answer = (f"检修工单 {wo_id}（{graph.work_orders[wo_id]['description']}）：\n"
              f"需断开开关：{', '.join(iso['isolate_switches']) or '无'}\n"
              f"防倒送电保持断开：{', '.join(iso['keep_open']) or '无'}")
    return {**state, "answer": answer, "trace": trace, "data": iso}


def status_node(state: AgentState) -> AgentState:
    overs = graph.check_overloads()
    open_sw = [s for s, c in _switch_state().items() if not c and s not in TIE_IDS]
    trace = ["[R1/R4 全图状态扫描]"]
    line1 = (f"当前断开的开关: {open_sw or '无'}"
             if open_sw else "当前所有开关均闭合, 全网正常供电")
    line2 = f"过载告警: {len(overs)} 条" if overs else "无过载设备"
    answer = f"电网运行状态：\n· {line1}\n· {line2}"
    return {**state, "answer": answer, "trace": trace, "data": {"open": open_sw, "overloads": overs}}


def fallback_node(state: AgentState) -> AgentState:
    answer = ("抱歉，我只能处理配电网推理性问题。可以这样问我：\n"
              "1. S1 断开后影响哪些用户？\n"
              "2. 配变 T1 过载怎么办？\n"
              "3. 检修配变 T1 需要断开哪些开关？\n"
              "4. 当前电网运行状态？\n"
              "（直接输入数字 1-5 也可快速提问）")
    return {**state, "answer": answer,
            "trace": ["意图无法识别, 拒绝作答(不硬编)"],
            "data": {"quick": list(QUICK_QUESTIONS.values())}}


def _switch_state() -> Dict[str, bool]:
    """当前开关状态快照(从图设备读取)"""
    return {sid: graph.devices[sid].get("isClosed", False) for sid in SWITCH_IDS}


# ---------------------------------------------------------------- 图构建
def route_node(state: AgentState) -> str:
    """条件路由: 按意图分发到对应推理节点"""
    return {
        "outage": "outage", "transfer": "transfer", "overload": "overload",
        "isolation": "isolation", "status": "status",
    }.get(state["intent"], "fallback")


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("parse", parse_node)
    g.add_node("outage", outage_node)
    g.add_node("transfer", transfer_node)
    g.add_node("overload", overload_node)
    g.add_node("isolation", isolation_node)
    g.add_node("status", status_node)
    g.add_node("fallback", fallback_node)
    g.set_entry_point("parse")
    g.add_conditional_edges("parse", route_node, {
        "outage": "outage", "transfer": "transfer", "overload": "overload",
        "isolation": "isolation", "status": "status", "fallback": "fallback",
    })
    for n in ("outage", "transfer", "overload", "isolation", "status", "fallback"):
        g.add_edge(n, END)
    return g.compile()


agent = build_graph()


def run_agent(question: str, devs: List[str] = None) -> dict:
    """执行 Agent 图, 返回 {intent, answer, trace, data}"""
    result = agent.invoke({"question": question, "devs": devs or [],
                           "intent": "", "answer": "", "trace": [], "data": {}})
    return {"intent": result.get("intent", "other"),
            "answer": result.get("answer", ""),
            "trace": result.get("trace", []),
            "data": result.get("data", {})}


if __name__ == "__main__":
    # 自检: 图构建成功 + 五类意图各跑一次
    print("Agent 图节点:", list(agent.get_graph().nodes))
    for q in ["S1 断开后影响哪些用户？", "配变 T1 过载怎么办？",
              "S2 断开后合上 L1 能否恢复供电？", "检修配变 T1 需要断开哪些开关？",
              "当前电网运行状态？", "今天天气怎么样？"]:
        r = run_agent(q)
        print(f"[{r['intent']}] {r['answer'][:40]}...")
