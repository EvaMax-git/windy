# A1: Word 文档入库 MVP 设计规格

**日期**: 2026-05-24
**状态**: Approved
**目标**: 实现 .docx 文件上传 → 自动解析 → 知识入库 → 可搜索的完整链路

---

## 1. 架构概览

```
前端上传 .docx
    ↓
POST /api/v4/import (multipart)
    ↓
[现有] staging → dedup → ingest_asset
    ↓
[新建] docx_parser.extract_text(file_path) → blocks[]
    ↓
[新建] auto_pipeline: create_blocks → chunk → FTS index
    ↓
完成，可搜索
```

## 2. 组件设计

### 2.1 `mneme/parsers/docx_parser.py`（新建）

```python
def extract_docx(file_path: Path) -> list[BlockDraft]:
    """从 .docx 文件提取结构化文本块"""
```

- 依赖: `python-docx>=1.1.0`
- 提取:
  - 标题 (Heading 1-3) → 标题块 (block_type="heading")
  - 段落 → 正文块 (block_type="paragraph")
  - 表格 → Markdown 表格块 (block_type="table")
- 输出: `BlockDraft(block_order: int, content_markdown: str, block_type: str)`
- 保持层级结构，标题作为分节标记

### 2.2 `mneme/parsers/__init__.py`（新建）

解析器注册表，按 MIME 类型分发:

```python
PARSER_REGISTRY: dict[str, Callable] = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": extract_docx,
}

def get_parser(mime_type: str) -> Callable | None:
    return PARSER_REGISTRY.get(mime_type)
```

### 2.3 `mneme/api/routes/knowledge/import_jobs.py`（修改）

在 `create_import_job` 中，ingest 完成后增加自动解析:

```python
# 现有: ingest_asset → processing_job → pipeline_run
# 新增: 如果有对应解析器，同步执行解析流水线
if parser := get_parser(asset_mime_type):
    blocks = parser(staged_file_path)
    # 创建 knowledge_document + blocks
    # 触发 chunking
    # 标记 FTS index ready
```

### 2.4 前端上传组件（增强）

- 上传进度反馈
- 解析状态显示（成功/失败/进行中）
- 完成后跳转知识文档详情页

## 3. 数据流

```
1. 用户选择 .docx 文件
2. POST /api/v4/import (multipart/form-data)
3. staging: 文件暂存 + SHA-256 去重
4. ingest_asset: 创建 inbox_item + asset + promote
5. get_parser(MIME) → extract_docx() → BlockDraft[]
6. create_document + add_blocks (自动)
7. chunk_document (paragraph 策略)
8. insert_chunks + mark_fts_ready
9. 返回 {asset_id, document_id, block_count, chunk_count}
```

## 4. 依赖

- `python-docx>=1.1.0` — 添加到 `pyproject.toml`

## 5. 验收标准

- [ ] 上传 .docx 文件后自动创建 knowledge_document
- [ ] 文档内容按标题/段落/表格分块
- [ ] 分块后自动创建 knowledge_chunks
- [ ] FTS 搜索可以找到入库内容
- [ ] 前端显示上传进度和解析状态
