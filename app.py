# -*- coding: utf-8 -*-
"""
配电网本体知识推理 Demo —— FastAPI 后端
=========================================
能力:
  1. GET  /                 前端页面(static/index.html)
  2. GET  /api/topology     电网拓扑图数据(含失电/过载高亮状态)
  3. GET  /api/ontology     本体类层次数据(供"本体可视化")
  4. POST /api/set_switch   切换开关状态 -> 触发推理 -> 返回最新拓扑
  5. POST /api/ask          Agent 对话推理(LangGraph 编排 + 可选 LLM 解析)
  6. GET  /api/ask_stream   SSE 流式返回推理链路
  7. GET  /api/neo4j        Neo4j 图数据库查询(可选, 未装时优雅降级)

架构说明(面试可讲):
  "符号推理 + LLM 混合" —— 对话层用 LangGraph StateGraph 编排
  (意图解析 -> 条件路由 -> 领域推理节点 -> 答案组装), 每个推理动作是图上的
  一个工具节点; 推理由确定性规则引擎完成(100% 可靠, 可溯源),
  LLM 只参与意图解析, 未配置时自动降级关键词规则, demo 可离线运行。

启动:  uvicorn app:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations
import json
import os
import time
from typing import Dict, List

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from agent_graph import graph, run_agent, _mentioned, SWITCH_IDS, TIE_IDS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ONTOLOGY_PATH = os.path.join(BASE_DIR, "ontology.yaml")

# 内存开关状态(演示用, 初始来自本体定义)
switch_state: Dict[str, bool] = {sid: graph.devices[sid]["isClosed"] for sid in SWITCH_IDS}
initial_switch_state: Dict[str, bool] = dict(switch_state)


def _effective_open_switches() -> List[str]:
    """当前所有断开的开关(排除常开联络开关, 它不是停电原因)"""
    return [s for s, c in switch_state.items() if not c and s not in TIE_IDS]


def _set_state(sw: str, closed: bool):
    switch_state[sw] = closed
    graph.devices[sw]["isClosed"] = closed


def _reset_state():
    for sid in SWITCH_IDS:
        switch_state[sid] = initial_switch_state[sid]
        graph.devices[sid]["isClosed"] = initial_switch_state[sid]


# ---------------------------------------------------------------- 拓扑数据
def build_topology() -> dict:
    open_sw = _effective_open_switches()
    outage = graph.compute_outage(open_sw) if open_sw else set()
    overloads = {o["device"] for o in graph.check_overloads()}

    nodes, edges = [], []
    seen_edges = set()

    # 变电站 / 母线
    for s in graph.substations.values():
        nodes.append({"id": s["id"], "name": s["name"], "category": "substation",
                      "status": "normal", "symbol": "rect", "symbolSize": [90, 36]})
    for b in graph.busbars.values():
        nodes.append({"id": b["id"], "name": b["name"], "category": "busbar",
                      "status": "normal"})

    for did, d in graph.devices.items():
        cat = {"switch": "switch", "transformer": "transformer",
               "linesegment": "lineseg"}.get(d["type"], "switch")
        if d.get("cls") == "TieSwitch":
            cat = "tie"
        status = "normal"
        if d.get("cls") in ("Breaker", "Sectionalizer") and not switch_state.get(did, True):
            status = "open"
        if did in outage:
            status = "outage"
        if did in overloads:
            status = "overload"
        node = {"id": did, "name": d.get("name", did), "category": cat,
                "status": status, "loadRate": d.get("loadRate"),
                "isClosed": switch_state.get(did, d.get("isClosed")),
                "feeder": d.get("feeder_of")}
        if d["type"] == "transformer":
            node["symbol"] = "diamond"
        nodes.append(node)

    # 拓扑边: upstream -> downstream(单向供电方向)，按馈线着色
    feeder_edge_color = {"F1": "#0A84FF", "F2": "#32ADE6"}
    for did, d in graph.devices.items():
        for up in d.get("upstream", []) or []:
            e = (up, did)
            if e not in seen_edges:
                seen_edges.add(e)
                fc = feeder_edge_color.get(d.get("feeder_of", ""), "#C7C7CC")
                edges.append({"source": up, "target": did,
                              "lineStyle": {"width": 2.5, "color": fc}})
    # 联络开关边(虚线, 跨馈线)
    for tid in TIE_IDS:
        tie = graph.devices[tid]
        a, b = tie.get("endA"), tie.get("endB")
        if a and b:
            edges.append({"source": a, "target": b, "label": {"show": True, "formatter": "L1", "position": "middle"},
                          "lineStyle": {"type": "dashed", "width": 3, "color": "#ff9800"}})

    return {"nodes": nodes, "edges": edges,
            "summary": {"outage_devices": sorted(outage),
                        "open_switches": open_sw}}


def build_ontology_view() -> dict:
    """本体类层次(供"本体可视化"面板): 类 + 继承 + 对象属性"""
    classes = []
    for c in graph.onto["classes"]:
        classes.append({"id": c["id"], "name": c.get("name", c["id"]),
                        "parent": c.get("parent")})
    props = [{"id": p["id"], "name": p.get("name", p["id"]),
              "domain": p.get("domain"), "range": p.get("range")}
             for p in graph.onto["object_properties"]]
    return {"classes": classes, "objectProperties": props,
            "ruleCount": len(graph.onto["rules"])}


# ---------------------------------------------------------------- FastAPI
app = FastAPI(title="配电网本体知识推理 Demo")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


class AskBody(BaseModel):
    question: str


class ToggleBody(BaseModel):
    device_id: str
    closed: bool


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/topology")
def api_topology():
    return build_topology()


@app.get("/api/ontology")
def api_ontology():
    return build_ontology_view()


@app.post("/api/set_switch")
def api_set_switch(body: ToggleBody):
    if body.device_id not in switch_state:
        return {"error": "未知开关"}
    _set_state(body.device_id, body.closed)
    return {"topology": build_topology(),
            "message": f"{body.device_id} 已{'合闸' if body.closed else '断开'}"}


@app.post("/api/reset")
def api_reset():
    _reset_state()
    return {"topology": build_topology(), "message": "已恢复初始状态"}


@app.post("/api/ask")
def api_ask(body: AskBody):
    """LangGraph Agent 对话推理"""
    return run_agent(body.question, _mentioned(body.question))


@app.get("/api/ask_stream")
def api_ask_stream(q: str = ""):
    """SSE 流式返回 LangGraph 推理链路"""
    result = run_agent(q, _mentioned(q))

    def gen():
        for step in result["trace"]:
            yield f"data: {json.dumps({'type': 'trace', 'step': step}, ensure_ascii=False)}\n\n"
            time.sleep(0.35)
        yield f"data: {json.dumps({'type': 'answer', 'answer': result['answer']}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


# ---------------------------------------------------------------- Neo4j 可选查询
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "neo4j123456")


@app.get("/api/neo4j")
def api_neo4j(sw: str = "S1"):
    """
    图数据库验证查询: 用 Cypher 可变长路径查断点下游设备。
    未安装/未启动 Neo4j 时优雅降级, 返回提示与规则引擎兜底结果。
    """
    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
        try:
            with driver.session() as s:
                rec = s.run(
                    f"MATCH p=(s:Device {{id:'{sw}'}})-[:DOWNSTREAM_OF*1..]->(d) "
                    "RETURN collect(DISTINCT d.id) AS downstream").single()
            cypher_downstream = sorted(rec["downstream"]) if rec else []
        finally:
            driver.close()
        engine_downstream = sorted(graph.compute_outage([sw]))
        return {"source": "neo4j", "switch": sw,
                "cypher_downstream": cypher_downstream,
                "engine_downstream": engine_downstream,
                "consistent": cypher_downstream == engine_downstream}
    except Exception as e:
        # 优雅降级: 未装 Neo4j 时用规则引擎兜底
        engine_downstream = sorted(graph.compute_outage([sw]))
        return {"source": "engine-fallback", "switch": sw,
                "engine_downstream": engine_downstream,
                "message": "Neo4j 未连接(先启动 Neo4j 并执行 python neo4j_import.py), "
                           "已用规则引擎兜底返回",
                "error": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
