# W1: 全部解析器跑通 — 设计规格

**日期**: 2026-05-25
**状态**: 待审批
**范围**: A-01 ~ A-08

---

## 1. 目标

为 Mneme 文档处理管道补齐四种文件解析器（PDF/Word/TXT/图片），实现文件类型自动判断，并确保 Docker 环境可构建。

## 2. 现状

| 组件 | 状态 |
|------|------|
| `mneme/parsers/__init__.py` | 已有 — PARSER_REGISTRY + get_parser() |
| `mneme/parsers/docx_parser.py` | 已有 — Word 解析，返回 `list[BlockDraft]` |
| `BlockDraft` dataclass | 已有 — `block_order`, `content_markdown`, `block_type` |
| `pyproject.toml` | 已有 `python-docx`, `pillow`, `jieba` |

## 3. 架构

```
mneme/parsers/
├── __init__.py          # 更新 — PARSER_REGISTRY 扩展 + detect_mime_type()
├── docx_parser.py       # 已有，不修改
├── pdf_parser.py        # 新增 — PyMuPDF (fitz)
├── txt_parser.py        # 新增 — chardet 编码检测
├── ocr_parser.py        # 新增 — PaddleOCR
└── mime_detect.py       # 新增 — 后缀→MIME 映射
```

### 3.1 统一接口

所有解析器遵循已有约定：

```python
def extract_xxx_bytes(content: bytes) -> list[BlockDraft]:
    """从 bytes 提取结构化文本块。"""
```

### 3.2 文件类型判断

`mime_detect.py` 提供：

```python
def detect_mime_type(filename: str, content: bytes) -> str:
    """通过后缀 + magic bytes 判断 MIME 类型。"""
```

优先级：后缀映射 → python-magic → 默认 `application/octet-stream`

## 4. 各解析器设计

### 4.1 TXT 解析器 (`txt_parser.py`)

- **依赖**: `chardet`（编码检测）
- **逻辑**: chardet 检测编码 → decode → 按行/段落分块
- **分块策略**: 空行分段，每段一个 BlockDraft
- **block_type**: `"paragraph"`

### 4.2 PDF 解析器 (`pdf_parser.py`)

- **依赖**: `pymupdf` (fitz)
- **逻辑**: fitz.open(stream=bytes) → 遍历页 → page.get_text()
- **中文**: PyMuPDF 原生支持中文，无需额外配置
- **分块策略**: 每页一个或多个 BlockDraft（按段落分割）
- **block_type**: `"paragraph"`

### 4.3 OCR 解析器 (`ocr_parser.py`)

- **依赖**: `paddleocr`, `paddlepaddle`
- **逻辑**: PaddleOCR(lang='ch') → ocr.ocr(image_bytes) → 合并文本行
- **准确率**: PaddleOCR 中文场景通常 > 90%
- **block_type**: `"paragraph"`
- **注意**: PaddleOCR 较大（~1GB），Docker 镜像需额外空间

### 4.4 文件类型判断 (`mime_detect.py`)

后缀映射表：

| 后缀 | MIME 类型 | 解析器 |
|------|-----------|--------|
| `.pdf` | `application/pdf` | `extract_pdf_bytes` |
| `.docx` | `application/vnd.openxmlformats-officedocument.wordprocessingml.document` | `extract_docx_bytes` |
| `.txt` | `text/plain` | `extract_txt_bytes` |
| `.md` | `text/markdown` | `extract_txt_bytes` |
| `.png/.jpg/.jpeg/.webp/.bmp` | `image/*` | `extract_image_bytes` |

## 5. 依赖变更

`pyproject.toml` 新增：

```toml
dependencies = [
    # ... existing ...
    "pymupdf>=1.24",       # PDF 解析
    "chardet>=5.0",        # 编码检测
    "paddleocr>=2.7",      # 图片 OCR
    "paddlepaddle>=2.5",   # PaddleOCR 后端
]
```

## 6. 测试策略

每个解析器对应一个测试文件：

| 测试文件 | 覆盖 |
|----------|------|
| `tests/test_txt_parser.py` | UTF-8/GBK/空文件/大文件 |
| `tests/test_pdf_parser.py` | 中文 PDF/多页 PDF/扫描件 |
| `tests/test_ocr_parser.py` | 中文图片/英文图片/低质量图片 |
| `tests/test_mime_detect.py` | 各种后缀/无后缀/magic bytes |
| `tests/test_docx_parser.py` | 已有，不修改 |

测试文件存放：`mneme_data/staging/` 或 `tests/fixtures/`

## 7. 验收标准

| 任务 | 验收 |
|------|------|
| A-01 | `pip install -e ".[dev]"` 成功，所有依赖可用 |
| A-02 | 每种类型至少一个测试文件 |
| A-03 | PDF 中文不乱码，全部页提取 |
| A-04 | Word 段落文字完整（已有测试通过） |
| A-05 | UTF-8/GBK 自适应 |
| A-06 | PaddleOCR 中文准确率 > 85% |
| A-07 | 后缀自动选解析器 |
| A-08 | `docker build` 成功 + 飞牛 NAS 可部署 |

## 8. 不做

- 不修改已有 `docx_parser.py`
- 不实现分块/清洗逻辑（属于模块二）
- 不实现加密（属于模块三）
- 不对接 NAS 存储（属于模块四）
