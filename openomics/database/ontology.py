import networkx as nx
import numpy as np
import obonet
import pandas as pd
from Bio.UniProt import GOA

from .base import Dataset
from ..utils.df import slice_adj


class Ontology(Dataset):
    def __init__(self, path, file_resources=None, col_rename=None, npartitions=0, verbose=False):
        self.network, self.node_list = self.load_network(file_resources)
        super(Ontology, self).__init__(path, file_resources, col_rename, npartitions, verbose)

    def load_network(self, file_resources) -> (nx.Graph, list):
        raise NotImplementedError

    def get_adjacency_matrix(self, node_list):
        if hasattr(self, "adjacency_matrix"):
            adjacency_matrix = self.adjacency_matrix
        else:
            adjacency_matrix = nx.adj_matrix(self.network, nodelist=node_list)
            self.adjacency_matrix = adjacency_matrix

        if node_list is None or list(node_list) == list(self.node_list):
            return adjacency_matrix
        elif set(node_list) < set(self.node_list):
            return slice_adj(adjacency_matrix, list(self.node_list), node_list, None)
        elif not (set(node_list) < set(self.node_list)):
            raise Exception("A node in node_list is not in self.node_list.")

        return adjacency_matrix

    def filter_network(self, namespace):
        raise NotImplementedError

    def filter_annotation(self, annotation: pd.Series):
        go_terms = set(self.node_list)
        filtered_annotation = annotation.map(
            lambda x: list(set(x) & go_terms) if isinstance(x, list) else [])

        return filtered_annotation

    def get_child_nodes(self):
        adj = self.get_adjacency_matrix(self.node_list)
        leaf_terms = self.node_list[np.nonzero(adj.sum(axis=0) == 0)[1]]
        return leaf_terms

    def get_root_nodes(self):
        adj = self.get_adjacency_matrix(self.node_list)
        parent_terms = self.node_list[np.nonzero(adj.sum(axis=1) == 0)[1]]
        return parent_terms

    def get_dfs_paths(self, root_nodes: list, filter_duplicates=False):
        """
        Return all depth-first search paths from root node(s) to children node by traversing the ontology directed graph.
        Args:
            root_nodes (list): ["GO:0008150"] if biological processes, ["GO:0003674"] if molecular_function, or ["GO:0005575"] if cellular_component
            filter_duplicates (bool): whether to remove duplicated paths that end up at the same leaf nodes

        Returns: pd.DataFrame of all paths starting from the root nodes.
        """
        if not isinstance(root_nodes, list):
            root_nodes = list(root_nodes)

        paths = list(dfs_path(self.network, root_nodes))
        paths = list(flatten_list(paths))

        paths_df = pd.DataFrame(paths)
        paths_df = paths_df[~paths_df.duplicated(keep="first")]

        if filter_duplicates:
            paths_df = filter_dfs_paths(paths_df)

        return paths_df

    def remove_predecessor_terms(self, annotation: pd.Series):
        leaf_terms = self.get_child_nodes()

        go_terms_parents = annotation.map(
            lambda x: list(set(x) & set(leaf_terms)) if isinstance(x, list) else [])
        return go_terms_parents

    def get_node_color(self, file="~/Bioinformatics_ExternalData/GeneOntology/go_colors_biological.csv"):
        go_colors = pd.read_csv(file)

        def selectgo(x):
            terms = [term for term in x if isinstance(term, str)]
            if len(terms) > 0:
                return terms[-1]
            else:
                return None

        go_colors["node"] = go_colors[[col for col in go_colors.columns if col.isdigit()]].apply(selectgo, axis=1)
        go_id_colors = go_colors[go_colors["node"].notnull()].set_index("node")["HCL.color"]
        go_id_colors = go_id_colors[~go_id_colors.index.duplicated(keep="first")]
        print(go_id_colors.unique().shape, go_colors["HCL.color"].unique().shape)
        return go_id_colors


class HumanPhenotypeOntology(Ontology):
    pass


class GeneOntology(Ontology):
    COLUMNS_RENAME_DICT = {
        "DB_Object_Symbol": "gene_name",
        "DB_Object_ID": "gene_id",
        "GO_ID": "go_id"
    }

    def __init__(self, path="http://geneontology.org/gene-associations/",
                 file_resources=None, col_rename=COLUMNS_RENAME_DICT, npartitions=0, verbose=False):
        """
        Handles downloading the latest Gene Ontology obo and annotation data, preprocesses them. It provide
        functionalities to create a directed acyclic graph of GO terms, filter terms, and filter annotations.
        """
        if file_resources is None:
            file_resources = {
                "go-basic.obo": "http://purl.obolibrary.org/obo/go/go-basic.obo",
                "goa_human.gaf": "goa_human.gaf.gz",
                "goa_human_rna.gaf": "goa_human_rna.gaf.gz",
                "goa_human_isoform.gaf": "goa_human_isoform.gaf.gz"
            }
        super(GeneOntology, self).__init__(path, file_resources, col_rename=col_rename, npartitions=npartitions,
                                           verbose=verbose)

    def info(self):
        print("network {}".format(nx.info(self.network)))

    def load_dataframe(self, file_resources):
        go_annotation_dfs = []
        for file in file_resources:
            if ".gaf" in file:
                go_lines = []
                for line in GOA.gafiterator(file_resources[file]):
                    go_lines.append(line)
                go_annotation_dfs.append(pd.DataFrame(go_lines))

        go_annotations = pd.concat(go_annotation_dfs)

        go_terms = pd.DataFrame.from_dict(self.network.nodes, orient='index', dtype="object")

        go_annotations["go_name"] = go_annotations["GO_ID"].map(go_terms["name"])
        go_annotations["namespace"] = go_annotations["GO_ID"].map(go_terms["namespace"])
        go_annotations["is_a"] = go_annotations["GO_ID"].map(go_terms["is_a"])

        return go_annotations

    def load_network(self, file_resources):
        for file in file_resources:
            if ".obo" in file:
                network = obonet.read_obo(file_resources[file])
                network = network.reverse(copy=True)
                node_list = np.array(network.nodes)
        return network, node_list

    def filter_network(self, namespace):
        terms = self.df[self.df["namespace"] == namespace]["go_id"].unique()
        print("{} terms: {}".format(namespace, len(terms))) if self.verbose else None
        self.network = self.network.subgraph(nodes=list(terms))
        self.node_list = np.array(list(terms))

    def get_predecessor_terms(self, annotation: pd.Series):
        go_terms_parents = annotation.map(
            lambda x: list({parent for term in x for parent in self.network.predecessors(term)}) \
                if isinstance(x, list) else [])
        return go_terms_parents

    def add_predecessor_terms(self, annotation: pd.Series, return_str=True):
        if annotation.dtypes == np.object and annotation.str.contains("\||;", regex=True).any():
            go_terms_annotations = annotation.str.split("|")
        else:
            go_terms_annotations = annotation

        go_terms_parents = go_terms_annotations + self.get_predecessor_terms(annotation)

        if return_str:
            go_terms_parents = go_terms_parents.map(
                lambda x: "|".join(x) if isinstance(x, list) else [])

        return go_terms_parents


def dfs_path(graph, path):
    node = path[-1]
    successors = list(graph.successors(node))
    if len(successors) > 0:
        for child in successors:
            yield list(dfs_path(graph, path + [child]))
    else:
        yield path


def flatten_list(list_in):
    if isinstance(list_in, list):
        for l in list_in:
            if isinstance(list_in[0], list):
                for y in flatten_list(l):
                    yield y
            elif isinstance(list_in[0], str):
                yield list_in
    else:
        yield list_in


def filter_dfs_paths(paths_df: pd.DataFrame):
    idx = {}
    for col in sorted(paths_df.columns[:-1], reverse=True):
        idx[col] = ~(paths_df[col].notnull() & paths_df[col].duplicated(keep="first") & paths_df[col + 1].isnull())

    idx = pd.DataFrame(idx)

    paths_df = paths_df[idx.all(axis=1)]
    return paths_df
