# Mneme 测试文档

## 测试文件说明

### W3/W4 新增测试 (本次开发)

| 测试文件 | 测试数 | 说明 |
|----------|--------|------|
| `tests/test_batch_encrypt.py` | 13 | 批量加密/解密测试 — 覆盖批量加密、批量解密、递归/非递归、空文件跳过、已加密文件跳过、目录树加密、错误处理 |
| `tests/test_directory_structure.py` | 15 | 公共/机密目录结构测试 — 覆盖目录创建、幂等性、路径解析、路径遍历防护、私密路径检测、目录状态统计 |
| `tests/test_watcher.py` | 14 | 文件监控测试 — 覆盖目录创建、去重（同文件/同内容）、状态持久化、文件类型过滤、隐藏文件跳过、私密目录检测 |
| `tests/test_facade.py` | 16 | 统一门面接口测试 — 覆盖 encrypt 门面（生成密钥/加解密往返/中文内容）、storage 门面（路径返回/公开/私密）、parser 门面（清洗/分块/完整处理/纯解析） |

### 已有测试 (W1/W2)

| 测试文件 | 说明 |
|----------|------|
| `tests/test_file_encrypt.py` | AES-256-GCM 文件加密测试 — 密钥生成、加解密往返、中文内容、错误密钥、篡改检测、Magic Header |
| `tests/test_storage_layer.py` | 存储层测试 — MIME 检测、文件暂存、内容哈希、路径安全 |
| `tests/test_cleaner.py` | 文字清洗测试 — 控制字符移除、空白规范化、页眉页脚移除 |
| `tests/test_parsers.py` | 解析器测试 — PDF/DOCX/TXT/OCR 解析 |
| `tests/test_pipeline.py` | 处理管道测试 — 完整 detect→parse→clean→chunk 流程 |

### 核心功能测试

| 测试文件 | 说明 |
|----------|------|
| `tests/test_auth.py` | 认证测试 — 密码哈希、会话令牌、登录/登出 |
| `tests/test_backup_restore.py` | 备份恢复测试 — pg_dump、清单验证、恢复预览 |
| `tests/test_conversations.py` | 对话测试 — 创建、消息、上下文组装 |
| `tests/test_dlq.py` | 死信队列测试 — 重试、恢复、清理 |
| `tests/test_gateway.py` | 网关测试 — Provider 路由、超时、重试 |
| `tests/test_health.py` | 健康检查测试 — 数据库/Redis/磁盘状态 |
| `tests/test_knowledge.py` | 知识库测试 — 文档 CRUD、搜索、索引 |
| `tests/test_memory.py` | 记忆测试 — 候选/正式记忆、版本、关系 |
| `tests/test_migration.py` | 迁移测试 — 发现、导出、导入 |
| `tests/test_search.py` | 搜索测试 — 全文搜索、向量搜索、图搜索 |
| `tests/test_vault.py` | 保险库测试 — Fernet 加密、访问日志 |
| `tests/test_worker.py` | Worker 测试 — 调度、消费、清扫、DLQ |

## 运行测试

```bash
# 运行全部测试
pytest

# 运行特定测试文件
pytest tests/test_facade.py -x -v

# 运行特定测试类
pytest tests/test_facade.py::TestEncryptFacade -v

# 运行特定测试函数
pytest tests/test_facade.py::TestEncryptFacade::test_encrypt_decrypt_roundtrip -v

# 遇错即停
pytest -x

# 显示覆盖率
pytest --cov=mneme --cov-report=html

# 并行运行（需安装 pytest-xdist）
pytest -n auto
```

## 测试数据

测试数据文件存放在 `tests/fixtures/` 目录：

| 文件 | 说明 |
|------|------|
| `sample_utf8.txt` | UTF-8 编码测试文本 |
| `sample_gbk.txt` | GBK 编码测试文本 |
| `sample.pdf` | PDF 测试文件 |
| `sample.docx` | Word 测试文件 |
| `sample.png` | 图片测试文件（用于 OCR） |

## 测试环境要求

- Python >= 3.10
- pytest >= 7.0
- pytest-asyncio >= 0.21
- testcontainers >= 4.0（集成测试需要 Docker）

## 编写新测试规范

1. 每个测试函数只测试一个行为
2. 测试名称使用 `test_<行为>_<条件>` 格式
3. 使用 `pytest.fixture` 管理测试数据
4. 使用 `tmp_path` fixture 处理临时文件
5. 断言使用明确的比较，避免 `assertTrue`
6. 测试异常使用 `pytest.raises` 上下文管理器
