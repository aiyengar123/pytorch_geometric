# %%
from profiling_utils import create_remote_backend_from_triplets
from rag_feature_store import SentenceTransformerFeatureStore
from rag_graph_store import NeighborSamplingRAGGraphStore
from torch_geometric.loader import RAGQueryLoader
from torch_geometric.nn.nlp import SentenceTransformer
from torch_geometric.datasets.updated_web_qsp_dataset import preprocess_triplet, retrieval_via_pcst
from torch_geometric.data import get_features_for_triplets_groups, Data
from itertools import chain
import torch
from typing import Tuple
import tqdm
import pandas as pd


# %%
triplets = torch.load('wikimultihopqa_full_graph.pt')

# %%
df = pd.read_csv('wikimultihopqa_cleaned.csv')
questions = df['question_text'].to_list()

# %%
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
model = SentenceTransformer(model_name='sentence-transformers/all-roberta-large-v1').to(device)

# %%
print("Generating remote backend...")
fs, gs = create_remote_backend_from_triplets(triplets=triplets, node_embedding_model=model, node_method_to_call="encode", path="backend", pre_transform=preprocess_triplet, node_method_kwargs={"batch_size": 256}, graph_db=NeighborSamplingRAGGraphStore, feature_db=SentenceTransformerFeatureStore).load()

# %%
print("Retrieving subgraphs...")
df_textual_nodes = pd.read_csv('wikimultihopqa_textual_nodes.csv')
df_textual_edges = pd.read_csv('wikimultihopqa_textual_edges.csv')

def apply_retrieval_via_pcst(graph: Data, query: str, topk: int = 3, topk_e: int = 3, cost_e: float = 0.5) -> Tuple[Data, str]:
    q_emb = model.encode(query)
    textual_nodes = df_textual_nodes.iloc[graph["node_idx"]].reset_index()
    textual_edges = df_textual_edges.iloc[graph["edge_idx"]].reset_index()
    out_graph, desc = retrieval_via_pcst(graph, q_emb, textual_nodes, textual_edges, topk, topk_e, cost_e)
    out_graph["desc"] = desc
    return out_graph

query_loader = RAGQueryLoader(data=(fs, gs), seed_nodes_kwargs={"k_nodes": 10}, seed_edges_kwargs={"k_edges": 10}, sampler_kwargs={"num_neighbors": [40]*3}, local_filter=apply_retrieval_via_pcst)

# %%
subgs = []
for subg in tqdm.tqdm(query_loader.batch_query(questions)):
    print(subg)
    subgs.append(subg)

torch.save(subgs, 'subg_results.pt')
