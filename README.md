# 核燃料循环质量与批次监管平台

本项目是一套可离线运行的 Python 服务端平台，用于管理数据中心、互联通道、加速卡资源批次、租户预约、容量分配、交付情景与硬件稳定性准入。业务状态、幂等结果和审计事件保存在 SQLite 中，适合调度、质量、风险和审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/compute_fabric/`：站点、通道、资源库存、预约、容量分配和情景分析；
- `src/accelerator_lab/`：加速卡测点导入、排除复核、分析任务租约和准入决定；
- `src/silicon_qualification/`：AI 加速芯片批次、测量、分析与质量审批；
- `src/nuclear_chain/`：核燃料循环批次监管——来源与成分声明、分批/合批、检测复检、质量放行与隔离、运输交接、不可逆处置、清单导入与谱系追溯；
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
PYTHONPATH=src python3 -m nuclear_chain.acceptance --workspace .
```

这些命令使用临时 SQLite 数据库完成站点、资源、预约、分配、测点分析、芯片准入和核燃料批次全生命周期流程，不访问外部网络。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m compute_fabric.api --database compute.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m accelerator_lab.api --database lab.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m silicon_qualification.api --database silicon.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m nuclear_chain.api --database nuclear.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。进程重启后可以继续查询 SQLite 中的业务状态和审计历史。

## 核燃料循环批次监管

`src/nuclear_chain/` 按角色（登记员、检测分析员、质量负责人、物流交接、资源回收、审计员）
管理材料批次全生命周期：

- **来源与成分声明**：登记供应商、来源地、质保文件与核素份额（份额之和必须为 1）；
  更正只追加新版本（`POST /batches/{id}/declarations`，乐观版本号），原始声明永久保留。
- **分批 / 合批**：数量守恒校验、结余数量扣减、谱系边与继承声明留痕；隔离状态向
  子批次粘性传播，合批结果必须重新检测。
- **检测与放行**：放行必须引用合格检测结论；已隔离批次须在隔离决定之后复检合格，
  质量负责人才可放行；拒收与隔离决定均只追加。
- **运输交接**：发运、接收、退回逐次留痕，记录单据与实测数量，支持幂等重放。
- **不可逆处置**：回收、弃置、最终贮存按结余数量整体执行，处置后任何变更一律拒绝。
- **清单导入**：`POST /manifests/import` 幂等批量登记；已隔离批次重复出现时只跳过
  并留痕（`manifest.quarantine_preserved`），状态绝不复活；其他重复批次冲突并整批回滚。
- **追溯视图**：`GET /batches/{id}/trace` 汇总声明版本、检测、决定、交接、处置和
  哈希链审计事件；`GET /batches/{id}/lineage` 用递归 CTE 给出全部派生批次，并单列
  已隔离与已不可逆处置的后代，父批次更正事件负载中同样带这两份清单。
- **审计链**：所有变更写入 SHA-256 哈希链（`GET /audit/chain` 可校验），写操作均带
  幂等键，重复请求重放首次响应，键相同内容不同则冲突。
