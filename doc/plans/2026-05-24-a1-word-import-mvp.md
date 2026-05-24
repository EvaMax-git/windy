# A1: Word 文档入库 MVP — 实施计划

**目标**: .docx 文件上传后自动解析为结构化知识块，可搜索
**架构**: 在现有 `asset_import_orchestrator` Step 5 中插入 MIME 类型分发，调用 docx_parser 提取结构化文本
**技术栈**: python-docx + 现有 chunking/FTS 引擎

---

## 任务清单

### Task 1: 添加 python-docx 依赖
**文件**: `pyproject.toml`

- [ ] 1.1 在 `dependencies` 中添加 `"python-docx>=1.1.0"`
- [ ] 1.2 运行 `pip install python-docx` 验证安装

### Task 2: 创建 docx 解析器
**新建文件**: `mneme/parsers/__init__.py`, `mneme/parsers/docx_parser.py`

- [ ] 2.1 创建 `mneme/parsers/__init__.py`，实现 `PARSER_REGISTRY` 和 `get_parser(mime_type)` 分发函数
- [ ] 2.2 创建 `mneme/parsers/docx_parser.py`，实现 `extract_docx(file_path) -> list[BlockDraft]`
  - 使用 `python-docx` 读取 .docx
  - 提取 Heading 1-3 → block_type="heading"
  - 提取段落 → block_type="paragraph"
  - 提取表格 → 转 Markdown 表格 → block_type="table"
  - 输出 `BlockDraft(block_order, content_markdown, block_type)`
- [ ] 2.3 创建 `tests/test_docx_parser.py`
  - 测试空文档
  - 测试纯文本文档
  - 测试含标题+段落+表格的文档
  - 测试中文内容

### Task 3: 修改 orchestrator 集成解析器
**修改文件**: `mneme/db/pipelines.py` (asset_import_orchestrator Step 5)

- [ ] 3.1 在 Step 5 开头，根据 `asset.media_type` 调用 `get_parser(mime_type)`
- [ ] 3.2 如果有对应解析器：调用解析器获取 BlockDraft[]，为每个 block 创建 `KnowledgeBlockCreate`，调用 `add_block`
- [ ] 3.3 如果没有解析器：保持现有纯文本解码逻辑作为 fallback
- [ ] 3.4 后续 chunk + FTS 流程不变（复用现有代码）
- [ ] 3.5 修改 `tests/test_pipelines.py` 或新建测试验证 docx 流程

### Task 4: 前端上传增强
**修改文件**: `mneme/web/src/pages/tabs/AssetTab.vue` 或相关上传组件

- [ ] 4.1 上传完成后显示解析状态（从 `/api/v4/import/{job_id}/status` 轮询）
- [ ] 4.2 解析成功后显示 block_count 和 chunk_count
- [ ] 4.3 添加"查看文档"链接跳转到知识文档详情

### Task 5: 端到端验证
- [ ] 5.1 准备测试 .docx 文件（含标题、段落、表格、中文）
- [ ] 5.2 通过 API 上传，验证返回 asset_id + document_id
- [ ] 5.3 通过 API 搜索入库内容，验证 FTS 命中
- [ ] 5.4 验证 Citation 溯源链完整

---

## 关键修改详情

### `mneme/parsers/docx_parser.py` 核心逻辑

```python
from docx import Document
from dataclasses import dataclass

@dataclass
class BlockDraft:
    block_order: int
    content_markdown: str
    block_type: str  # "heading", "paragraph", "table"

def extract_docx(file_path: Path) -> list[BlockDraft]:
    doc = Document(str(file_path))
    blocks = []
    order = 0

    for para in doc.paragraphs:
        if para.style.name.startswith("Heading"):
            level = para.style.name.replace("Heading ", "")
            blocks.append(BlockDraft(order, f"{'#' * int(level)} {para.text}", "heading"))
            order += 1
        elif para.text.strip():
            blocks.append(BlockDraft(order, para.text, "paragraph"))
            order += 1

    for table in doc.tables:
        md = _table_to_markdown(table)
        blocks.append(BlockDraft(order, md, "table"))
        order += 1

    return blocks
```

### `mneme/db/pipelines.py` Step 5 修改

```python
# 现有: text = content_bytes.decode(enc)
# 修改为:
from mneme.parsers import get_parser

parser = get_parser(asset.media_type)
if parser:
    blocks_draft = parser(storage_path)
    # 为每个 BlockDraft 创建 KnowledgeBlockCreate
    for draft in blocks_draft:
        add_block(db, ..., block_type=draft.block_type, content_markdown=draft.content_markdown)
    # 后续 chunk + FTS 不变
else:
    # fallback: 现有纯文本解码逻辑
    text = content_bytes.decode(enc)
    ...
```

---

## 验收标准

- [ ] 上传 .docx → 自动创建 knowledge_document + blocks
- [ ] blocks 按标题/段落/表格结构化分块
- [ ] chunk 后自动创建 knowledge_chunks + FTS 索引
- [ ] `GET /api/v4/knowledge/search?q=关键词` 能命中 .docx 中的内容
- [ ] Citation 溯源链 chunk → block → document → asset 完整
- [ ] 前端上传后显示解析状态和结果统计
