import torch
import pickle
import torch.utils.data
import time
import os
import numpy as np
from torch_geometric.utils import get_laplacian
import csv
from scipy import sparse as sp
import dgl
from dgl.data import TUDataset
from dgl.data import LegacyTUDataset
import torch_geometric as pyg
from scipy.sparse import csr_matrix
import random
random.seed(42)

from sklearn.model_selection import StratifiedKFold, train_test_split
from torch_geometric.data import InMemoryDataset
import csv
import json

class pygFormDataset(torch.utils.data.Dataset):
    """
        DGLFormDataset wrapping graph list and label list as per pytorch Dataset.
        *lists (list): lists of 'graphs' and 'labels' with same len().
    """
    def __init__(self, *lists):
        assert all(len(lists[0]) == len(li) for li in lists)
        self.lists = lists
        self.node_lists = lists[0]
        self.node_labels = lists[1]

    def __getitem__(self, index):
        return tuple(li[index] for li in self.lists)

    def __len__(self):
        return len(self.lists[0])

def format_dataset(dataset):
    """
        Utility function to recover data,
        INTO-> dgl/pytorch compatible format
    """
    nodes = [data[0] for data in dataset]
    labels = [data[1] for data in dataset]


    return pygFormDataset(nodes, labels)

class NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return json.JSONEncoder.default(self, obj)

def get_all_split_idx(dataset):
    """
        - Split total number of graphs into 3 (train, val and test) in 80:10:10
        - Stratified split proportionate to original distribution of data with respect to classes
        - Using sklearn to perform the split and then save the indexes
        - Preparing 10 such combinations of indexes split to be used in Graph NNs
        - As with KFold, each of the 10 fold have unique test set.
    """
    root_idx_dir = './data/planetoid/'
    if not os.path.exists(root_idx_dir):
        os.makedirs(root_idx_dir)

    # If there are no idx files, do the split and store the files
    if not os.path.exists(root_idx_dir + f"{dataset.name}_splits.json"):
        print("[!] Splitting the data into train/val/test ...")
        all_idxs = np.arange(dataset[0].num_nodes)
        # Using 10-fold cross val to compare with benchmark papers
        k_splits = 10

        cross_val_fold = StratifiedKFold(n_splits=k_splits, shuffle=True)
        k_data_splits = []
        split = {"train": [], "val": [], "test": []}
        for train_ok_split, test_ok_split in cross_val_fold.split(X = all_idxs, y = dataset[0].y):
            # split = {"train": [], "val": [], "test": all_idxs[test_ok_split]}
            train_ok_targets = dataset[0].y[train_ok_split]
            # Gets final 'train' and 'val'
            train_i_split, val_i_split = train_test_split(train_ok_split,
                                                 test_size=0.111,
                                                 stratify=train_ok_targets)
            # Extracting only idxs
            split['train'].append(train_i_split)
            split['val'].append(val_i_split)
            split['test'].append(all_idxs[test_ok_split])
        filename = root_idx_dir + f"{dataset.name}_splits.json"
        with open(filename, "w") as f:
            json.dump(split, f, cls=NumpyEncoder)  # , cls=NumpyEncoder
        print("[!] Splitting done!")

    # reading idx from the files
    with open(root_idx_dir + f"{dataset.name}_splits.json", "r") as fp:
        all_idx = json.load(fp)
    return all_idx


class DGLFormDataset(torch.utils.data.Dataset):
    """
        DGLFormDataset wrapping graph list and label list as per pytorch Dataset.
        *lists (list): lists of 'graphs' and 'labels' with same len().
    """
    def __init__(self, *lists):
        assert all(len(lists[0]) == len(li) for li in lists)
        self.lists = lists
        self.graph_lists = lists[0]
        self.graph_labels = lists[1]

    def __getitem__(self, index):
        return tuple(li[index] for li in self.lists)

    def __len__(self):
        return len(self.lists[0])



def self_loop(g):
    """
        Utility function only, to be used only when necessary as per user self_loop flag
        : Overwriting the function dgl.transform.add_self_loop() to not miss ndata['feat'] and edata['feat']
        
        
        This function is called inside a function in TUsDataset class.
    """
    new_g = dgl.DGLGraph()
    new_g.add_nodes(g.number_of_nodes())
    new_g.ndata['feat'] = g.ndata['feat']
    
    src, dst = g.all_edges(order="eid")
    src = dgl.backend.zerocopy_to_numpy(src)
    dst = dgl.backend.zerocopy_to_numpy(dst)
    non_self_edges_idx = src != dst
    nodes = np.arange(g.number_of_nodes())
    new_g.add_edges(src[non_self_edges_idx], dst[non_self_edges_idx])
    new_g.add_edges(nodes, nodes)
    
    # This new edata is not used since this function gets called only for GCN, GAT
    # However, we need this for the generic requirement of ndata and edata
    new_g.edata['feat'] = torch.zeros(new_g.number_of_edges())
    return new_g

def positional_encoding(g, pos_enc_dim, framework = 'pyg'):
    """
        Graph positional encoding v/ Laplacian eigenvectors
    """
    # Laplacian,for the pyg
    if framework == 'pyg':
        L = get_laplacian(g.edge_index,normalization='sym',dtype = torch.float64)
        L = csr_matrix((L[1], (L[0][0], L[0][1])), shape=(g.num_nodes, g.num_nodes))
        # Eigenvectors with scipy
        # EigVal, EigVec = sp.linalg.eigs(L, k=pos_enc_dim+1, which='SR')
        EigVal, EigVec = sp.linalg.eigs(L, k=pos_enc_dim + 1, which='SR', tol=1e-2)  # for 40 PEs
        EigVec = EigVec[:, EigVal.argsort()]  # increasing order
        pos_enc = torch.from_numpy(EigVec[:, 1:pos_enc_dim + 1].astype(np.float32)).float()
        return pos_enc
        # add astype to discards the imaginary part to satisfy the version change pytorch1.5.0
    elif framework == 'dgl':
        A = g.adjacency_matrix_scipy(return_edge_ids=False).astype(float)
        N = sp.diags(dgl.backend.asnumpy(g.in_degrees()).clip(1) ** -0.5, dtype=float)
        L = sp.eye(g.number_of_nodes()) - N * A * N
        # Eigenvectors with scipy
        # EigVal, EigVec = sp.linalg.eigs(L, k=pos_enc_dim+1, which='SR')
        EigVal, EigVec = sp.linalg.eigs(L, k=pos_enc_dim + 1, which='SR', tol=1e-2)  # for 40 PEs
        EigVec = EigVec[:, EigVal.argsort()]  # increasing order
        g.ndata['pos_enc'] = torch.from_numpy(EigVec[:, 1:pos_enc_dim + 1].astype(np.float32)).float()
        # add astype to discards the imaginary part to satisfy the version change pytorch1.5.0

    
class PlanetoidDataset(InMemoryDataset):
    def __init__(self, name, use_node_embedding = False):
        t0 = time.time()
        self.name = name
        data_dir = 'data/planetoid'
        #dataset = TUDataset(self.name, hidden_size=1)
        # dataset = LegacyTUDataset(self.name, hidden_size=1) # dgl 4.0
        self.dataset = pyg.datasets.Planetoid(root=data_dir, name= name ,split = 'full')

        print("[!] Dataset: ", self.name)
        if use_node_embedding:
            embedding = torch.load(data_dir + '/embedding_'+name + '.pt', map_location='cpu')
            # self.dataset.data.x = embedding
            # self.laplacian = positional_encoding(self.dataset[0], 200, framework = 'pyg')
            self.dataset.data.x = torch.cat([self.dataset.data.x, embedding], dim=-1)

        # this function splits data into train/val/test and returns the indices
        self.all_idx = get_all_split_idx(self.dataset)
        edge_feat_dim = 1
        self.edge_attr = torch.ones(self.dataset[0].num_edges, edge_feat_dim)
        # self.all = dataset
        # dataset.train[split_number]
        self.train_idx = [torch.tensor(self.all_idx['train'][split_num], dtype=torch.long) for split_num in range(10)]
        self.val_idx = [torch.tensor(self.all_idx['val'][split_num], dtype=torch.long) for split_num in range(10)]
        self.test_idx = [torch.tensor(self.all_idx['test'][split_num], dtype=torch.long) for split_num in range(10)]
        # self.train = [self.format_dataset([dataset[idx] for idx in self.all_idx['train'][split_num]]) for split_num in range(10)]
        # self.val = [self.format_dataset([dataset[idx] for idx in self.all_idx['val'][split_num]]) for split_num in range(10)]
        # self.test = [self.format_dataset([dataset[idx] for idx in self.all_idx['test'][split_num]]) for split_num in range(10)]
        
        print("Time taken: {:.4f}s".format(time.time()-t0))
    
    def format_dataset(self, dataset):  
        """
            Utility function to recover data,
            INTO-> dgl/pytorch compatible format 
        """
        graphs = [data[0] for data in dataset]
        labels = [data[1] for data in dataset]

        for graph in graphs:
            #graph.ndata['feat'] = torch.FloatTensor(graph.ndata['feat'])
            graph.ndata['feat'] = graph.ndata['feat'].float() # dgl 4.0
            # adding edge features for Residual Gated ConvNet, if not there
            if 'feat' not in graph.edata.keys():
                edge_feat_dim = graph.ndata['feat'].shape[1] # dim same as node feature dim
                graph.edata['feat'] = torch.ones(graph.number_of_edges(), edge_feat_dim)

        return DGLFormDataset(graphs, labels)
    
    
    # form a mini batch from a given list of samples = [(graph, label) pairs]
    def collate(self, samples):
        # The input samples is a list of pairs (graph, label).
        graphs, labels = map(list, zip(*samples))
        labels = torch.tensor(np.array(labels))
        #tab_sizes_n = [ graphs[i].number_of_nodes() for i in range(len(graphs))]
        #tab_snorm_n = [ torch.FloatTensor(size,1).fill_(1./float(size)) for size in tab_sizes_n ]
        #snorm_n = torch.cat(tab_snorm_n).sqrt()  
        #tab_sizes_e = [ graphs[i].number_of_edges() for i in range(len(graphs))]
        #tab_snorm_e = [ torch.FloatTensor(size,1).fill_(1./float(size)) for size in tab_sizes_e ]
        #snorm_e = torch.cat(tab_snorm_e).sqrt()
        batched_graph = dgl.batch(graphs)
        
        return batched_graph, labels
    
    
    # prepare dense tensors for GNNs using them; such as RingGNN, 3WLGNN
    def collate_dense_gnn(self, samples):
        # The input samples is a list of pairs (graph, label).
        graphs, labels = map(list, zip(*samples))
        labels = torch.tensor(np.array(labels))
        #tab_sizes_n = [ graphs[i].number_of_nodes() for i in range(len(graphs))]
        #tab_snorm_n = [ torch.FloatTensor(size,1).fill_(1./float(size)) for size in tab_sizes_n ]
        #snorm_n = tab_snorm_n[0][0].sqrt()  
        
        #batched_graph = dgl.batch(graphs)
    
        g = graphs[0]
        adj = self._sym_normalize_adj(g.adjacency_matrix().to_dense())        
        """
            Adapted from https://github.com/leichen2018/Ring-GNN/
            Assigning node and edge feats::
            we have the adjacency matrix in R^{n x n}, the node features in R^{d_n} and edge features R^{d_e}.
            Then we build a zero-initialized tensor, say T, in R^{(1 + d_n + d_e) x n x n}. T[0, :, :] is the adjacency matrix.
            The diagonal T[1:1+d_n, i, i], i = 0 to n-1, store the node feature of node i. 
            The off diagonal T[1+d_n:, i, j] store edge features of edge(i, j).
        """

        zero_adj = torch.zeros_like(adj)
        
        in_dim = g.ndata['feat'].shape[1]
        
        # use node feats to prepare adj
        adj_node_feat = torch.stack([zero_adj for j in range(in_dim)])
        adj_node_feat = torch.cat([adj.unsqueeze(0), adj_node_feat], dim=0)
        
        for node, node_feat in enumerate(g.ndata['feat']):
            adj_node_feat[1:, node, node] = node_feat

        x_node_feat = adj_node_feat.unsqueeze(0)
        
        return x_node_feat, labels
    
    def _sym_normalize_adj(self, adj):
        deg = torch.sum(adj, dim = 0)#.squeeze()
        deg_inv = torch.where(deg>0, 1./torch.sqrt(deg), torch.zeros(deg.size()))
        deg_inv = torch.diag(deg_inv)
        return torch.mm(deg_inv, torch.mm(adj, deg_inv))
    
    
    def _add_self_loops(self):

        # function for adding self loops
        # this function will be called only if self_loop flag is True
        for split_num in range(10):
            self.train[split_num].graph_lists = [self_loop(g) for g in self.train[split_num].graph_lists]
            self.val[split_num].graph_lists = [self_loop(g) for g in self.val[split_num].graph_lists]
            self.test[split_num].graph_lists = [self_loop(g) for g in self.test[split_num].graph_lists]
            
        for split_num in range(10):
            self.train[split_num] = DGLFormDataset(self.train[split_num].graph_lists, self.train[split_num].graph_labels)
            self.val[split_num] = DGLFormDataset(self.val[split_num].graph_lists, self.val[split_num].graph_labels)
            self.test[split_num] = DGLFormDataset(self.test[split_num].graph_lists, self.test[split_num].graph_labels)
