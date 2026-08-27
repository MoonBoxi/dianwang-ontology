# -*- coding: utf-8 -*-
"""
Neo4j 图数据库导入脚本
=======================
把 ontology.yaml 中的配电网实例数据导入 Neo4j:
  - 节点: Device(开关/配变/线路段/母线, 带 cls/type/负荷属性), Customer, WorkOrder
  - 关系: DOWNSTREAM_OF(拓扑下游) / SERVES(配变->用户) / COVERS(工单->设备)

用法:
  1. 启动 Neo4j(社区版默认 bolt://localhost:7687, 默认账号 neo4j/neo4j)
  2. python neo4j_import.py --uri bolt://localhost:7687 --user neo4j --password 你的密码
  3. 验证: python neo4j_import.py --check

导入后可用 Cypher 验证拓扑推理(与 Python 规则引擎结果一致):
  MATCH p=(s:Device{id:'S1'})-[:DOWNSTREAM_OF*1..]->(d)
  RETURN collect(DISTINCT d.id) AS downstream
"""
from __future__ import annotations
import argparse
import sys

from inference_engine import PowerGraph


def import_to_neo4j(uri: str, user: str, password: str) -> int:
    from neo4j import GraphDatabase
    graph = PowerGraph("ontology.yaml")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    n_dev, n_rel = 0, 0
    try:
        with driver.session() as s:
            s.run("MATCH (n) DETACH DELETE n")            # 清空重导
            # ---- 设备节点(开关/配变/线路段/母线) ----
            for did, d in graph.devices.items():
                s.run(
                    "CREATE (n:Device {id:$id, name:$name, cls:$cls, type:$type, "
                    "isClosed:$isClosed, loadRate:$loadRate})",
                    id=did, name=d.get("name", did), cls=d.get("cls", ""),
                    type=d["type"], isClosed=d.get("isClosed", None),
                    loadRate=d.get("loadRate", None))
                n_dev += 1
            # ---- 变电站/馈线/用户/工单 ----
            for bid, b in graph.busbars.items():
                s.run("CREATE (n:Device {id:$id, name:$name, cls:'Busbar', type:'busbar'})",
                      id=bid, name=b.get("name", bid)); n_dev += 1
            for sid, st in graph.substations.items():
                s.run("CREATE (n:Substation {id:$id, name:$name})", id=sid, name=st["name"])
            for fid, f in graph.feeders.items():
                s.run("CREATE (n:Feeder {id:$id, name:$name, ratedCapacity:$rc, currentLoad:$cl})",
                      id=fid, name=f["name"], rc=f["ratedCapacity"], cl=f.get("currentLoad", 0))
            for cid, c in graph.customers.items():
                s.run("CREATE (n:Customer {id:$id, name:$name, customerCount:$cc})",
                      id=cid, name=c.get("name", cid), cc=c.get("customerCount", 0))
            for wid, w in graph.work_orders.items():
                s.run("CREATE (n:WorkOrder {id:$id, description:$desc})",
                      id=wid, desc=w.get("description", ""))

            # ---- 关系: DOWNSTREAM_OF(拓扑核心) ----
            for did, d in graph.devices.items():
                for nxt in d.get("downstream", []) or []:
                    s.run("MATCH (a:Device {id:$a}),(b:Device {id:$b}) "
                          "CREATE (a)-[:DOWNSTREAM_OF]->(b)", a=did, b=nxt)
                    n_rel += 1
            # 母线 -> 首端设备
            for bid, b in graph.busbars.items():
                for nxt in b.get("downstream", []) or []:
                    s.run("MATCH (a:Device {id:$a}),(b:Device {id:$b}) "
                          "CREATE (a)-[:DOWNSTREAM_OF]->(b)", a=bid, b=nxt)
                    n_rel += 1
            # 联络开关连接
            for tid, tie in graph.tie_switches.items():
                for end in (tie.get("endA"), tie.get("endB")):
                    if end:
                        s.run("MATCH (a:Device {id:$a}),(b:Device {id:$b}) "
                              "CREATE (a)-[:CONNECTS]->(b)", a=tid, b=end)
                        n_rel += 1
            # ---- 关系: SERVES(配变->用户) / COVERS(工单->设备) ----
            for tid, t in graph.devices.items():
                for cid in t.get("serves", []) or []:
                    s.run("MATCH (a:Device {id:$a}),(b:Customer {id:$b}) "
                          "CREATE (a)-[:SERVES]->(b)", a=tid, b=cid)
                    n_rel += 1
            for wid, w in graph.work_orders.items():
                for eid in w.get("coveredEquipment", []) or []:
                    s.run("MATCH (a:WorkOrder {id:$a}),(b:Device {id:$b}) "
                          "CREATE (a)-[:COVERS]->(b)", a=wid, b=eid)
                    n_rel += 1
        print(f"✅ 导入完成: {n_dev} 个设备节点, {n_rel} 条关系")
        return 0
    except Exception as e:
        print(f"❌ 导入失败(请确认 Neo4j 已启动且账号密码正确): {e}")
        return 1
    finally:
        driver.close()


def check(uri: str, user: str, password: str) -> int:
    """验证: 用 Cypher 查 S1 下游设备, 应等于规则引擎结果"""
    from neo4j import GraphDatabase
    graph = PowerGraph("ontology.yaml")
    expected = sorted(graph.compute_outage(["S1"]))
    driver = GraphDatabase.driver(uri, auth=(user, password))
    try:
        with driver.session() as s:
            rec = s.run(
                "MATCH p=(s:Device {id:'S1'})-[:DOWNSTREAM_OF*1..]->(d) "
                "RETURN collect(DISTINCT d.id) AS downstream").single()
            actual = sorted(rec["downstream"])
        print(f"✅ Cypher 查询 S1 下游: {actual}")
        print(f"✅ 规则引擎预期:     {expected}")
        print("✅ 图数据库查询与规则引擎结果一致!" if actual == expected
              else "❌ 结果不一致, 请检查导入!")
        return 0 if actual == expected else 1
    except Exception as e:
        print(f"❌ 验证失败: {e}")
        return 1
    finally:
        driver.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="配电网本体实例导入 Neo4j")
    ap.add_argument("--uri", default="bolt://localhost:7687")
    ap.add_argument("--user", default="neo4j")
    ap.add_argument("--password", default="neo4j123456")
    ap.add_argument("--check", action="store_true", help="仅验证导入结果")
    args = ap.parse_args()
    sys.exit(check(args.uri, args.user, args.password) if args.check
             else import_to_neo4j(args.uri, args.user, args.password))
