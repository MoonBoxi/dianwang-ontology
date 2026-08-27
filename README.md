# 配电网本体模型构建与知识推理 —— 交付说明

## 一、交付物清单（对应题目 5 项要求 + 技术增强）

| # | 题目要求 | 交付文件 | 状态 |
|---|---|---|---|
| 1 | 本体模型设计 | `ontology.yaml`（19 类含 4 层继承 / 32 数据属性 / 23 对象属性 / 9 条规则含 SWRL 风格公理） | ✅ |
| 2 | 推理验证（5 问） | `verification.py`（13 断言）+ `test_suite.py`（14 断言，共 27 项全 PASS） | ✅ |
| 3 | 本体可视化 | `static/index.html` 本体模型页（类继承树 + 属性 + 规则） | ✅ |
| 4 | 设计文档（800 字） | `design_doc.md` | ✅ |
| 5 | Demo（可视化 + Agent 对话） | `app.py` + `agent_graph.py` + `static/index.html` | ✅ |
| 增强 | LangGraph 对话编排 | `agent_graph.py`（StateGraph 状态机：意图解析→条件路由→推理节点） | ✅ |
| 增强 | Neo4j 图数据库 | `neo4j_import.py` + `/api/neo4j`（Cypher 查询验证，未装时优雅降级） | ✅ |
| 增强 | Docker 部署 | `Dockerfile` + `docker-compose.yml` | ✅ |
| 配套 | 测试报告 | `test_report.md`（41/41 断言通过） | ✅ |
| 配套 | **与模型交互记录** | `人机交互记录.md`（10 轮真实会话实录：提问→意图→推理链路→回答→交互思路） | ✅ |

## 二、快速启动

项目已配置专用 conda 环境 `dianwang`（Python 3.10，位于 `D:\conda25\envs\dianwang`），依赖已安装：

```bash
# 方式一：conda 环境（推荐，已配置好）
conda activate dianwang
cd "C:\Users\28133\Desktop\作业"

python verification.py                          # 运行推理验证（应输出 ALL 5 QUESTIONS PASSED）
python test_suite.py                            # 边界场景测试（14 项）
uvicorn app:app --host 0.0.0.0 --port 8000      # 启动 Demo

# 方式二：Docker 部署
docker compose up --build                       # 一键启动 app + neo4j，访问 http://localhost:8000
```

浏览器打开 http://localhost:8000：
- **拓扑推理页**：左侧拓扑图——**点击任何开关节点切换 合/分 状态**，自动触发规则推理，失电设备变灰红描边、过载变红；右侧对话区可提问，支持 5 类推理问题与 SSE 流式链路演示
- **本体模型页**：类继承树（PowerSystemResource→Equipment→ConductingEquipment→Switch→…）+ 对象属性 + 规则说明

## 三、LangGraph 对话编排（agent_graph.py）

对话层已用 **LangGraph StateGraph** 重构：

```
parse(意图解析, 规则/可选LLM)
   │
   ├─[outage]→     outage节点(规则R1/R2/R3/R9 失电分析)
   ├─[transfer]→   transfer节点(规则R6/R7 负荷转移判定)
   ├─[overload]→   overload节点(规则R4/R5 过载触发)
   ├─[isolation]→  isolation节点(规则R8 检修隔离)
   ├─[status]→     status节点(全图状态)
   └─[other]→      fallback节点(礼貌拒答, 不硬编)
```

每个推理动作是图上的一个**工具节点**，由确定性规则引擎执行；LLM 只参与意图解析（可选）。这是"符号推理 + LLM 混合"架构的 LangGraph 落地，State 状态管理 + 条件路由 + 可插拔节点，新增意图只需加节点和路由。

## 四、Neo4j 图数据库（可选增强）

未启动 Neo4j 时 `/api/neo4j` 自动降级为规则引擎兜底（不影响任何功能）。启动后：

```bash
# 1. 启动 Neo4j(本地或 docker compose up neo4j)，默认 bolt://localhost:7687
# 2. 导入本体实例数据
python neo4j_import.py --password neo4j123456
# 3. 验证 Cypher 与规则引擎一致性
python neo4j_import.py --check
# 4. 浏览器访问 http://localhost:8000/api/neo4j?sw=S1 查看图数据库查询结果
```

导入后可用 Cypher 做图查询，例如 `S1 的下游设备`（可变长路径）：
```cypher
MATCH p=(s:Device {id:'S1'})-[:DOWNSTREAM_OF*1..]->(d) RETURN collect(DISTINCT d.id)
```
`--check` 会自动比对 Cypher 查询与 Python 规则引擎结果是否一致（应一致）。

## 五、接入 LLM 解析自然语言（可选）

默认使用关键词规则解析意图（零依赖、离线可跑）。配置 LLM 后，对话将先用 LLM 解析 `{意图, 设备参数}`，再由推理引擎做确定性计算：

```bash
# Windows: set LLM_API_KEY=sk-xxx
export LLM_API_KEY=sk-xxx          # 任意 OpenAI 兼容接口
export LLM_BASE_URL=https://api.deepseek.com/v1
export LLM_MODEL=deepseek-chat
```

## 六、演示电网拓扑（自洽设计，可手算验证）

```
                    [联络开关L1: 常开, 连接 LsegX ↔ LsegB]
                              │
南城变电站 Sub1               │
  └─ 母线 Bus1 ─ CB1 ─ T0 ─ S1 ─ LsegA ─ S2 ─ LsegX ─┬─ T1 ─ C1(500户)
      (电源)     (断路器) (配变) (分段)        (分段)    ├─ T2 ─ C2(300户)
                  │         │                         └─(LsegA分支)T3 ─ C3(200户)
                  │         └─ C0(100户)
                  └─ CB2 ─ S3 ─ LsegB ─┬─ T4 ─ C4(400户)
                        (馈线F2)        └─ T5 ─ C5(300户)
```

**容量设定**：T0 100/200kVA·T1 560/630(88.9%过载)·T2 300/400·T3 200/315·T4 500/630·T5 250/315；F1 额定 2000（当前 1160），F2 额定 1600（当前 750）；配变过载阈值 80%，馈线过载阈值 90%，转供安全容量系数 90%。

## 七、5 个推理问题预期答案（verification.py 已验证）

| # | 问题 | 推理规则 | 预期答案 |
|---|---|---|---|
| Q1 | S1 断开后影响哪些用户？ | R1→R2→R3 | T1/T2/T3 失电，1000 户停电 |
| Q2 | 配变 T1 过载，能否经 L1 转 F2？ | R4→R6→R7 | 可行，合闸后 F2 负载率 81.9% |
| Q3 | CB1 跳闸影响多少用户？ | R1→R2→R3 | 1100 户（含 T0） |
| Q4 | 检修 T1 需断开哪些开关？ | R8 | 断开 S2，保持 L1 断开防倒送 |
| Q5 | S2 断开合 L1 能否恢复？ | R6→R7 | 拒绝：F2 将过载至 100.6% |

## 八、系统架构

```
用户提问 ──► LangGraph Agent (parse 节点: 规则/可选 LLM 意图解析)
                 │ 条件路由 (StateGraph)
                 ├─ outage / transfer / overload / isolation / status 节点
                 └─ 每个节点调用推理引擎(确定性规则 R1~R9)
                 │
  前端(ECharts 拓扑) ◄── /api/topology ◄─────────── 失电/过载高亮
  对话界面 ◄──────── /api/ask ──► {answer, trace} ──► 结果聚合 R2/R3
```

设计亮点：**符号推理 + LLM 混合**——推理 100% 由确定性规则引擎完成（可溯源、可手算复核），LLM 只负责翻译自然语言；这正是纯 RAG 无法回答"推理性问题"的解决方案。对话层以 LangGraph 状态机编排，每个推理动作是可插拔的工具节点，新增意图只需加节点与路由。
