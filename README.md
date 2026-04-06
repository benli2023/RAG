这份仓库的测试与验收说明已独立到 [TESTS.md](TESTS.md)。README 只保留项目配置、启动说明和其他非测试内容，方便快速浏览。

## 配置说明

Elasticsearch 版本要求：

- 最低可用版本建议为 `7.17`。
- 更稳妥的推荐版本为 `8.x`。
- 当前项目只使用基础索引、查询和删除接口，不依赖旧版 `2.x` 行为。

本地安装建议使用官方 tarball，并放到用户目录下，避免把大文件提交到仓库：

```bash
mkdir -p "$HOME/.local/elasticsearch"
curl -fL "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz" \
    -o "$HOME/.local/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz"
tar -xzf "$HOME/.local/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz" \
    -C "$HOME/.local/elasticsearch"
rm "$HOME/.local/elasticsearch/elasticsearch-9.3.2-darwin-aarch64.tar.gz"
```

安装完成后，Elasticsearch 的根目录就是：

```bash
$HOME/.local/elasticsearch/elasticsearch-9.3.2
```

可以直接让启动脚本使用这个路径：

```bash
ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.3.2" ./scripts/start_elasticsearch.sh
```

这个脚本会复制一份干净的本地配置，然后以单节点、关闭安全认证的开发模式启动 Elasticsearch，方便后端直接用 `http://localhost:9200` 连接，也避免和第一次自动生成的安全配置冲突。

如果是第一次启动，建议把等待时间调长一点，避免 Elasticsearch 还在初始化时就被脚本判定失败：

```bash
ES_STARTUP_TIMEOUT=180 ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.3.2" ./scripts/start_elasticsearch.sh
```

如果你想手动启动，也可以直接执行：

```bash
"$HOME/.local/elasticsearch/elasticsearch-9.3.2/bin/elasticsearch"
```

Windows users can use the bundled repository copy directly from PowerShell:

```powershell
.\scripts\start_elasticsearch_windows.ps1
.\scripts\stop_elasticsearch_windows.ps1
```

By default, the Windows scripts look for `./elasticsearch-9.3.2` under the project root. You can still override that with `ES_HOME` or `ELASTICSEARCH_BIN` if you want to point at a separate installation.

布尔配置统一收口在 `backend/rag_config.py`，支持“文件内默认值 + 环境变量覆盖”：

```python
ENABLE_ACL = _get_bool_config("ENABLE_ACL", False)
RERANKER_ENABLED = _get_bool_config("RERANKER_ENABLED", True)
ENABLE_SUB_CHUNKING = _get_bool_config("ENABLE_SUB_CHUNKING", False)
```

这三个开关现在都遵循同一套规则：

- 直接修改 `backend/rag_config.py` 可以作为项目默认配置。
- 启动前设置同名环境变量，可以临时覆盖默认值。

Reranker 是否启用可以直接在 `backend/rag_config.py` 中配置：

```python
# 是否启用 BGE Reranker 精排
# 可直接修改这里；如需按环境覆盖，可设置 RERANKER_ENABLED=true/false
RERANKER_ENABLED = _get_bool_config("RERANKER_ENABLED", True)
```

默认行为：

- `RERANKER_ENABLED = True` 时，检索会先走向量召回，再走 CrossEncoder 精排。
- `RERANKER_ENABLED = False` 时，系统只保留向量召回排序，不加载 reranker 模型。
- 关闭 reranker 后，`RERANK_CANDIDATE_K` 会自动回退到 `RETRIEVAL_K`，避免无意义扩大候选集。

如果你想临时覆盖配置，也可以在启动前设置环境变量：

```bash
RERANKER_ENABLED=false python backend/server.py
```

嵌入模型也可以通过环境变量覆盖。默认会优先加载本地的 `my_local_bge_m3`，如果 Windows 的内存/分页文件不足导致加载失败，会自动回退到更轻量的 `intfloat/multilingual-e5-small`：

```bash
EMBEDDING_MODEL_NAME=./my_local_bge_m3
EMBEDDING_FALLBACK_MODEL_NAME=intfloat/multilingual-e5-small
EMBEDDING_DEVICE=cpu
```

如果你本机的分页文件太小，优先增大虚拟内存仍然是最稳妥的做法；这个回退只是为了让服务尽量能启动并继续工作。

ACL 和子分块的临时覆盖方式相同：

```bash
ENABLE_ACL=true ENABLE_SUB_CHUNKING=true python backend/server.py
```

另外可以通过只读接口查看当前生效配置：

```bash
GET /config
```

这个接口会返回当前布尔开关、检索参数、模型配置、路径配置，以及每项配置当前来自配置文件还是环境变量，便于前端展示和排查问题。