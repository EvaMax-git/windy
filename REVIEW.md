# REVIEW.md — Mneme3 代码审查规范

> 版本: v128 | 生效: 2026-05-08 | 维护: zyys

---

## 1. P0 / P1 / P2 判定规则

### P0 — 阻断（Blocker）

**定义**：违反 Mneme v128 五条铁律、导致数据丢失、事务不一致、安全漏洞、或导致 199 ARM64 无法部署。

| 触发条件 | 示例 |
|----------|------|
| 铁律违反 | 业务写入与 audit/outbox 分离、Agent 直写正式 memory、Gateway 旁路直连 |
| 数据安全 | 事务内不写 audit_events、idempotency 缺失导致重复写、敏感字段入日志 |
| 部署阻断 | Python 版本不兼容 3.10（199 裸机约束）、ARM64 前端 build 失败 |
| 数据完整性 | 表结构错误导致查询空结果、API 路径双前缀导致 404 |
| 安全漏洞 | secret 入全文索引、凭证明文入库、未授权访问受保护端点 |

**处理**：**阻断验收，必须修复**。不允许以"后续 Phase 再补"免责。

### P1 — 重要（Important）

**定义**：影响正式写入正确性、安全契约、性能退化、维护性严重受损、199 内存风险。

| 触发条件 | 示例 |
|----------|------|
| 事务边界模糊 | 跨模块调用无明确事务声明、异步链路无幂等键 |
| 性能风险 | 199 1.9GB RAM 上 Worker 独立进程致 OOM、PostgreSQL shared_buffers 未优化 |
| 维护性严重退化 | 单文件承载 4 个业务域、重复 DB 模块长期未清理 |
| 路由完整性 | 注册端点路径与文档不一致、SSE/嵌套路由无明确文档 |
| 缺失中间件 | 无监控告警、磁盘使用率不可见、无迁移 dry-run |
| 降级逻辑缺失 | Redis 不可用时 Worker 仅 standby（无 DB polling 降级）、Gateway 无本地 fallback |

**处理**：**阻断对应工作包，必须修复**。若不影响 Phase 门禁整体，可记录为 CONDITIONAL PASS。

### P2 — 建议（Suggestion）

**定义**：不影响功能正确性和安全底线，但影响用户体验、代码整洁度、长期可维护性。

| 触发条件 | 示例 |
|----------|------|
| 前端体验 | 无首次登录引导、30 天 redirect 窗口偏短、无 i18n 机制 |
| 代码整洁 | 重复 DB 模块未清理、未使用导入未删除 |
| 文档缺失 | 新 API 无 OpenAPI schema、DDL 无注释 |
| 性能优化 | COUNT 查询无物化视图（< 10 万行时 tolerable）、日志轮转未配置 |
| 测试覆盖 | 特定边界条件未覆盖、E2E 测试缺失 |

**处理**：**Phase 内尽量修复；若不影响门禁，可列入下一 Phase**。

---

## 2. 三位审查员分工

### 审查模型

Mneme3 采用三 Agent 协作审查模型：

```
coding_agent ──→ 实现代码、单元测试、文档同步
review_agent ──→ 架构审查、安全审查、事务审查、数据模型一致性
test_agent  ──→ 测试计划、合同测试、集成测试、门禁验收
```

### coding_agent（主实现）

| 维度 | 职责 |
|------|------|
| 编码 | 所有 Phase 任务包的代码实现 |
| 测试编写 | 单元测试（与实现同步） |
| 文档 | README、CLAUDE.md、doc/ 下全部文档 |
| 审查配合 | 对 review_agent 发现的问题提供修复 |
| 部署 | Docker Compose / 199 裸机 / systemd 配置 |
| 不在职责 | 集成测试设计、验收报告输出 |

### review_agent（架构审查）

| 轮次 | 时机 | 审查重点 |
|------|------|----------|
| Round A | 环境 + 数据模型完成后 | DDL 与 64 表一致性、索引/外键/CHECK/vector 类型 |
| Round B | API / Auth / Policy 完成后 | Session 与 Agent Token 分离、Policy 接口合同、统一响应包络 |
| Round C | Audit / Outbox / Object 完成后 | 三表同事务、idempotency、object version 追溯链 |
| Round D | 运行骨架 + 最终门禁 | Worker/dispatcher 边界清晰、health/heartbeat 语义正确、Phase 2 待办明确 |

每轮产出：**审查意见清单**（P0/P1/P2 分级 + 具体文件位置 + 修复建议）。

### test_agent（测试验收）

| 层 | 范围 | 产出 |
|-----|------|------|
| 单元测试 | Policy / schema / idempotency / audit helper | 覆盖率报告 |
| 集成测试 | PostgreSQL+pgvector 真实环境 / Alembic / Auth / 写链路 / Object Registry | 测试通过清单 |
| Smoke / 门禁 | Docker Compose 一键启动 / health / worker no-op / OpenAPI 生成 | 门禁通过/失败 |
| 验收报告 | Phase 终审 | PASS / CONDITIONAL PASS / FAIL 判定 + P0/P1 关闭证据 |

---

## 3. Phase 门禁审查清单

### 通用门禁（所有 Phase 共用）

- [ ] 所有写 API 同事务写业务表 + `audit_events` + `events`
- [ ] 幂等键正确实现（`Idempotency-Key` / `job_key` / `event_id + consumer_name`）
- [ ] 统一响应包络（`code` / `message` / `data` / `request_id`）
- [ ] 结构化日志含 `request_id` / `actor_type` / `route` / `duration_ms`
- [ ] 敏感字段（password / token / secret）不入日志、不入全文索引
- [ ] 199 ARM64 部署验证通过（Python 3.10 / pgvector / 前端 build）
- [ ] 测试命令可一键执行（`pytest` 全量或指定文件）

### Phase 1 — Core Platform
- [ ] Alembic 64 表在真实 PG16+pgvector 环境 `upgrade head` 成功
- [ ] `user_sessions` 与 `agent_tokens` 物理分离
- [ ] Policy Engine `can() -> decision` 四种返回值全覆盖
- [ ] `X-Request-Id` 自动生成 + 透传

### Phase 2 — Runtime Platform
- [ ] Redis 不可用时业务写入仍落 PG outbox
- [ ] Backup/Restore dry-run 至少一次演练通过
- [ ] Review 承接高风险动作（敏感访问/高成本调用/导入/恢复确认）
- [ ] DLQ 5 类 failure_class 全覆盖

### Phase 3 — Storage + Asset + Pipeline
- [ ] 上传失败原件保留 + 可追踪
- [ ] Pipeline 失败可重试、不重复写正式对象
- [ ] 资产敏感等级 + 路径净化 + 审计链完整

### Phase 4 — Knowledge OS
- [ ] 编辑后正文立即正确
- [ ] 搜索/embedding 未追平时有 stale 状态暴露
- [ ] Citation 定位到 block/source_map 级别

### Phase 5 — Importer + Migration
- [ ] 资产/知识数量零误差
- [ ] 文件 hash 抽样通过
- [ ] 正式切换 + 回滚演练报告

### Phase 6 — Memory OS
- [ ] 候选/正式/版本/来源四层分离
- [ ] 无 `activated_by_review_item_id` 的 memory 不可 active
- [ ] 搜索降级到 FTS 并暴露 stale 状态

### Phase 7 — Agent + Context Compiler
- [ ] Context Compile 每次生成 `context_packs` + `context_pack_items`
- [ ] Agent 只能提交 candidate，不能直写正式 memory
- [ ] Context Compile 失败时回退到基础搜索

### Phase 8 — Graph + Eval
- [ ] 图谱更新失败不阻断主业务链
- [ ] Graph 明确显示版本滞后状态
- [ ] 知识检索/记忆候选/权限拦截三类 eval 覆盖

---

## 4. 缺陷处理协议

```
发现缺陷 → 分级(P0/P1/P2)
    ├─ P0: 立即阻断，review_agent 标注 #BLOCKER
    │     → coding_agent 修复 → review_agent 复审 → test_agent 回归
    │
    ├─ P1: 阻断对应工作包，review_agent 标注 #IMPORTANT
    │     → coding_agent 排期修复 → review_agent 复审
    │     → 若不影响门禁 → CONDITIONAL PASS + Phase N+1 跟踪
    │
    └─ P2: 记录但不阻断，review_agent 标注 #SUGGESTION
          → Phase 内尽量修复 → 未修复项进入 backlog
```

---

## 5. 关联文档

| 文档 | 路径 | 用途 |
|------|------|------|
| Agent 上岗 | `CLAUDE.md` | 构建命令 + 铁律 + 事务模式 + 代码风格 |
| 架构基线 | `doc/架构基线.md` | 唯一权威架构裁决（含Phase路线+运维手册+多模态预设计） |
| 数据模型 | `doc/数据模型.md` | 64 表完整 DDL（基线45 + 后续迁移19） |
| 一致性设计 | `doc/一致性设计.md` | 事务边界 / 幂等键定义 / DLQ 分类 |
| v128 发布说明 | `doc/v128-发布说明.md` | v128 新增能力 + 问题修复 + 部署验证 |
| 迁移参考 | `doc/迁移参考.md` | Mneme2 交互分析（迁移参考） |
