这份仓库的测试与验收说明已独立到 [TESTS.md](TESTS.md)。README 只保留项目配置、启动说明和其他非测试内容，方便快速浏览。

## 配置说明

Elasticsearch 版本要求：

- 最低可用版本建议为 `7.17`。
- 更稳妥的推荐版本为 `8.x`。
- 当前项目只使用基础索引、查询和删除接口，不依赖旧版 `2.x` 行为。

本地安装建议使用官方 tarball，并放到用户目录下，避免把大文件提交到仓库：

当前仓库默认建议使用 `9.1.4`，因为 `analysis-ik` 已有对应发布包，可直接安装中文分词插件。

```bash
mkdir -p "$HOME/.local/elasticsearch"
curl -fL "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.1.4-darwin-aarch64.tar.gz" \
    -o "$HOME/.local/elasticsearch/elasticsearch-9.1.4-darwin-aarch64.tar.gz"
tar -xzf "$HOME/.local/elasticsearch/elasticsearch-9.1.4-darwin-aarch64.tar.gz" \
    -C "$HOME/.local/elasticsearch"
rm "$HOME/.local/elasticsearch/elasticsearch-9.1.4-darwin-aarch64.tar.gz"
```

安装 Elasticsearch 之后，还需要安装匹配版本的 `analysis-ik` 插件，否则中文 BM25 会退回到标准分词，检索质量会明显下降。插件版本必须和 Elasticsearch 版本一致。

```bash
"$HOME/.local/elasticsearch/elasticsearch-9.1.4/bin/elasticsearch-plugin" install --batch \
    https://get.infini.cloud/elasticsearch/analysis-ik/9.1.4
```

安装完成后可以检查插件是否已加载：

```bash
"$HOME/.local/elasticsearch/elasticsearch-9.1.4/bin/elasticsearch-plugin" list
```

如果输出里包含 `analysis-ik`，就可以继续启动 Elasticsearch。

安装完成后，Elasticsearch 的根目录就是：

```bash
$HOME/.local/elasticsearch/elasticsearch-9.1.4
```

可以直接让启动脚本使用这个路径：

```bash
ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.1.4" ./scripts/start_elasticsearch.sh
```

这个脚本会复制一份干净的本地配置，然后以单节点、关闭安全认证的开发模式启动 Elasticsearch，方便后端直接用 `http://localhost:9200` 连接，也避免和第一次自动生成的安全配置冲突。

如果你是第一次安装，推荐按下面顺序执行：

1. 解压 Elasticsearch 9.1.4。
2. 安装 `analysis-ik` 插件。
3. 启动 Elasticsearch。
4. 用 `_analyze` 接口验证 `ik_max_word` 能正常分词。

示例验证命令：

```bash
curl -s http://localhost:9200/_analyze \
    -H 'Content-Type: application/json' \
    -d '{"analyzer":"ik_max_word","text":"安装成功"}'
```

如果插件安装成功，返回里会包含多个中文词元，而不是按单字或标准英文分词切开。

如果是第一次启动，建议把等待时间调长一点，避免 Elasticsearch 还在初始化时就被脚本判定失败：

```bash
ES_STARTUP_TIMEOUT=180 ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.1.4" ./scripts/start_elasticsearch.sh
```

如果你想手动启动，也可以直接执行：

```bash
"$HOME/.local/elasticsearch/elasticsearch-9.1.4/bin/elasticsearch"
```

### Red Hat / RHEL / Rocky / AlmaLinux

如果你在 Red Hat 系发行版上部署，推荐同样使用官方 tarball 安装到用户目录，再安装匹配版本的 `analysis-ik` 插件。下面以 `x86_64` 为例；如果是 ARM 机器，把下载地址里的 `x86_64` 改成 `aarch64`。

如果目标机器是离线环境，先在一台可联网机器上把下面两个文件下载好，再拷贝到 Red Hat 主机：

1. Elasticsearch 9.1.4 的 tar 包。
2. 与 Elasticsearch 9.1.4 匹配的 `analysis-ik` 离线安装包，文件名以发布页实际下载结果为准。

可以参考上游发布页：`https://release.infinilabs.com/analysis-ik/stable/`。

离线安装时，不要再访问在线安装地址，直接使用本地文件安装插件。

1. 安装必要工具。

```bash
sudo dnf install -y curl tar
```

2. 下载并解压 Elasticsearch 9.1.4。

```bash
mkdir -p "$HOME/.local/elasticsearch"
curl -fL "https://artifacts.elastic.co/downloads/elasticsearch/elasticsearch-9.1.4-linux-x86_64.tar.gz" \
    -o "$HOME/.local/elasticsearch/elasticsearch-9.1.4-linux-x86_64.tar.gz"
tar -xzf "$HOME/.local/elasticsearch/elasticsearch-9.1.4-linux-x86_64.tar.gz" \
    -C "$HOME/.local/elasticsearch"
rm "$HOME/.local/elasticsearch/elasticsearch-9.1.4-linux-x86_64.tar.gz"
```

3. 安装匹配版本的 `analysis-ik` 插件。

```bash
"$HOME/.local/elasticsearch/elasticsearch-9.1.4/bin/elasticsearch-plugin" install --batch \
    file:///absolute/path/to/analysis-ik-9.1.4.zip
```

4. 启动 Elasticsearch。

```bash
ES_HOME="$HOME/.local/elasticsearch/elasticsearch-9.1.4" ./scripts/start_elasticsearch.sh
```

5. 验证 IK 是否可用。

```bash
curl -s http://localhost:9200/_analyze \
    -H 'Content-Type: application/json' \
    -d '{"analyzer":"ik_max_word","text":"中华人民共和国"}'
```

如果返回多个中文词元，就说明 Red Hat 环境里的 IK 插件已经生效。

Windows users can use the bundled repository copy directly from PowerShell:

```powershell
.\scripts\start_elasticsearch_windows.ps1
.\scripts\stop_elasticsearch_windows.ps1
```

By default, the Windows scripts look for `./elasticsearch-9.1.4` under the project root. You can still override that with `ES_HOME` or `ELASTICSEARCH_BIN` if you want to point at a separate installation.

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

backend 现在固定通过 RPC 调用远端检索服务，不再在本地进程里加载 embedding 模型。下面这些 embedding 环境变量应当配置在 `RAG-RPC` 服务端，而不是 `backend/server.py`：

```bash
EMBEDDING_MODEL_NAME=./my_local_bge_m3
EMBEDDING_FALLBACK_MODEL_NAME=intfloat/multilingual-e5-small
EMBEDDING_DEVICE=cpu
```

如果你在 RPC 节点上遇到模型加载内存问题，优先增大虚拟内存仍然是最稳妥的做法；这些变量不会再影响 backend 进程本身。

ACL 和子分块的临时覆盖方式相同：

```bash
ENABLE_ACL=true ENABLE_SUB_CHUNKING=true python backend/server.py
```

如果你要按用户组限制可见的知识库，可以编辑 `backend/knowledge_base_group_mapping.json`。这个文件定义的是“用户组 -> 知识库列表”的允许关系，和现有的 `backend/username_group_mapping.json` 配合使用。

示例：

```json
{
    "iam-admin": ["shop", "shop1"],
    "order-admin": ["shop"],
    "user-support": ["shop1"]
}
```

规则如下：

- `username_group_mapping.json` 负责“用户名 -> 用户组”。
- `knowledge_base_group_mapping.json` 负责“用户组 -> 允许访问的知识库”。
- 当 `ENABLE_ACL=true` 时，`/retrieve` 会先检查用户是否有当前知识库权限，再执行检索。
- 如果 `knowledge_base_group_mapping.json` 为空或不存在，知识库访问会回退为不限制，便于逐步启用。

另外可以通过只读接口查看当前生效配置：

```bash
GET /config
```

这个接口会返回当前布尔开关、检索参数、模型配置、路径配置，以及每项配置当前来自配置文件还是环境变量，便于前端展示和排查问题。

## 独立部署 gRPC 检索微服务

本项目支持将检索链路下沉到 gRPC 微服务独立部署。独立部署后，主 RAG 服务通过安全的 TLS 加密套接字远程连接子服务发起向量检索、BM25 融合检索、reranker 精排以及向量入库等操作。

### 1. 所需依赖包

在独立部署 gRPC server 的服务器上，需要安装检索链路相关依赖：

```bash
pip install grpcio grpcio-tools fastapi python-frontmatter langchain-core langchain-text-splitters langchain-chroma langchain-community langchain-huggingface pydantic sentence-transformers elasticsearch
```

### 2. 证书生成

微服务强制使用 SSL 加密传输。如果你没有公共证书，可以在 `RAG-RPC` 目录下生成一张自签发证书：

```bash
cd RAG-RPC
mkdir -p certs
openssl req -x509 -newkey rsa:4096 -keyout certs/server.key -out certs/server.crt -days 365 -nodes -subj '/CN=localhost'
```

### 3. 服务端启动

独立部署时请直接从 `RAG-RPC` 目录启动服务：

```bash
cd RAG-RPC
python server.py
```
服务器默认启动于 `50051` 端口，等待接收安全的 gRPC 请求。默认会把 Chroma 数据目录解析到 `RAG-RPC/chroma_db`；如果你需要改目录，可以显式设置 `DB_DIR`。`RAG-RPC/server.py` 不再回退依赖项目根目录下的数据库或检索实现。

### 4. 主 RAG 服务连接配置

在主要的 RAG 部署环境里，backend 现在默认且仅支持通过远程 gRPC 连接工作。你需要确保 `certs/server.crt` 在 RAG 客户端所在节点可以被访问到；`/retrieve`、入库和向量读写都会走 RPC 端，不再保留本地回退方案。

```bash
REMOTE_DB_TARGET=localhost:50051
REMOTE_DB_CERT=certs/server.crt
python backend/server.py
```