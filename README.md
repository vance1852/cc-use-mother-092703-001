# 核燃料循环质量与批次监管平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景、硬件稳定性准入，以及核燃料循环材料的来源声明、分批/合批、检测复核、放行决定、运输交接与不可逆处置追溯。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/fuel_cycle/`：核燃料循环批次监管——来源与成分声明、分批/合批、检测复核、
  有权人员放行/隔离决定、运输交接、不可逆处置、谱系追溯与更正影响分析；
- `fixtures/`：离线验收使用的结构化协议与测点；
- `tests/`：核心规则、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m compute_fabric.acceptance --workspace .
PYTHONPATH=src python3 -m accelerator_lab.acceptance --workspace .
PYTHONPATH=src python3 -m silicon_qualification.acceptance
PYTHONPATH=src python3 -m fuel_cycle.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入，以及
登记→检测→四眼放行→分批→父信息更正→影响分析→复检再放行→运输交接→合批回收→
不可逆处置的完整批次监管流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m fuel_cycle.api --database fuel.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 批次监管服务（fuel_cycle）核心规则

- **统一批次标识**：声明、检测、决定、交接、分批/合批、处置全部以 `batch_id` 关联，
  派生关系以输入/输出边构成谱系 DAG；
- **原始依据只追加**：来源与成分声明更正产生新版本（旧版本与依据摘要原样保留），
  检测、放行/隔离决定、交接、处置全部只追加，全部事件进入全局哈希链审计；
- **防重复导入复活**：来源凭证（`source_type`+`source_reference`）全局唯一登记，
  登记接口以 `Idempotency-Key` 幂等；重复导入只回放原响应或被拒绝，绝不触碰状态，
  已隔离/已处置材料不会因此回到可用状态；
- **状态机**：`declared → released ⇄ quarantined → in_transit`（交接冻结）
  `→ exhausted`（分批耗尽）/`disposed`（不可逆终态）；
- **放行门禁**：放行须依据基于**本人及全部祖先当前声明版本**的合格检测，
  且检测人不得是放行人（四眼分离）；
- **更正级联**：声明更正后，基于旧信息的放行（含派生批次）自动撤回为隔离，
  必须复检后才能重新放行；在途批次在接收时校验，依据失效则回落隔离；
- **影响分析**：`GET /batches/{id}/impact` 列出全部派生批次是否受更正影响、
  是否已复检重新验证、是否已进入不可逆处置。
