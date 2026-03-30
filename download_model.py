import os

from sentence_transformers import SentenceTransformer
from sentence_transformers.cross_encoder import CrossEncoder

# 使用阿里云 Hugging Face 镜像，提升国内下载稳定性
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

# 如需更高限速与更稳定下载，可提前在系统环境变量中设置 HF_TOKEN
hf_token = os.getenv("HF_TOKEN")
if hf_token:
	print("检测到 HF_TOKEN，将使用认证方式下载。")
else:
	print("未检测到 HF_TOKEN，将以匿名方式下载，可能会有速率限制提示。")

EMBEDDING_MODEL_NAME = "BAAI/bge-m3"
RERANKER_MODEL_NAME = "BAAI/bge-reranker-base"


def download_embedding_model() -> None:
	print("开始下载 Embedding 模型，可能需要几分钟...")
	model = SentenceTransformer(EMBEDDING_MODEL_NAME, token=hf_token)
	model.save("./my_local_bge_m3")
	print("Embedding 模型下载完成，已保存在 ./my_local_bge_m3")


def download_reranker_model() -> None:
	print("开始下载 Reranker 模型，可能需要几分钟...")
	model = CrossEncoder(RERANKER_MODEL_NAME, token=hf_token)
	model.save("./my_local_bge_reranker")
	print("Reranker 模型下载完成，已保存在 ./my_local_bge_reranker")


if __name__ == "__main__":
	download_embedding_model()
	download_reranker_model()