"""
Метрики качества разбиения графа для сравнения алгоритмов сегментации VLAN
"""

import networkx as nx
from typing import Dict, List, Any
from collections import defaultdict

def conductance(graph: nx.Graph, partition: Dict[int, int]) -> float:
    """
    Расчет conductance для разбиения графа.
    φ(S) = |∂S| / min(vol(S), vol(S̄))
    """
    if not partition or len(partition) == 0:
        return float('inf')
    
    clusters: Dict[int, List[int]] = defaultdict(list)
    for node, cluster_id in partition.items():
        clusters[cluster_id].append(node)
    
    if len(clusters) <= 1:
        return 0.0
    
    total_conductance = 0.0
    
    for cluster_id, nodes in clusters.items():
        cluster_set = set(nodes)
        cut_edges = 0
        vol_cluster = 0
        
        for node in nodes:
            vol_cluster += graph.degree(node)
            for neighbor in graph.neighbors(node):
                if neighbor not in cluster_set:
                    cut_edges += 1
        
        all_nodes = set(graph.nodes())
        complement = all_nodes - cluster_set
        vol_complement = sum(graph.degree(node) for node in complement)
        
        if vol_cluster > 0 and vol_complement > 0:
            cluster_conductance = cut_edges / min(vol_cluster, vol_complement)
            total_conductance += cluster_conductance
    
    return total_conductance / len(clusters)


def normalized_cut(graph: nx.Graph, partition: Dict[int, int]) -> float:
    """
    Расчет Normalized Cut для разбиения.
    NCut(S) = cut(S,S̄)/vol(S) + cut(S,S̄)/vol(S̄)
    """
    if not partition or len(partition) == 0:
        return float('inf')
    
    clusters: Dict[int, List[int]] = defaultdict(list)
    for node, cluster_id in partition.items():
        clusters[cluster_id].append(node)
    
    if len(clusters) <= 1:
        return 0.0
    
    total_ncut = 0.0
    
    for cluster_id, nodes in clusters.items():
        cluster_set = set(nodes)
        cut_edges = 0
        vol_cluster = 0
        
        for node in nodes:
            vol_cluster += graph.degree(node)
            for neighbor in graph.neighbors(node):
                if neighbor not in cluster_set:
                    cut_edges += 1
        
        all_nodes = set(graph.nodes())
        complement = all_nodes - cluster_set
        vol_complement = sum(graph.degree(node) for node in complement)
        
        if vol_cluster > 0 and vol_complement > 0:
            cluster_ncut = cut_edges / vol_cluster + cut_edges / vol_complement
            total_ncut += cluster_ncut
    
    return total_ncut / len(clusters)


def modularity(graph: nx.Graph, partition: Dict[int, int]) -> float:
    """Расчет модулярности разбиения."""
    return nx.community.modularity(graph, list(partition.values()))


def calculate_metrics(graph: nx.Graph, partition: Dict[int, int]) -> Dict[str, Any]:
    """Расчет всех метрик качества разбиения."""
    if not partition:
        return {
            'conductance': float('inf'),
            'normalized_cut': float('inf'),
            'modularity': 0.0,
            'num_clusters': 0,
            'num_nodes': 0,
            'avg_cluster_size': 0,
            'cluster_sizes': {}
        }
    
    clusters = defaultdict(list)
    for node, cluster_id in partition.items():
        clusters[cluster_id].append(node)
    
    return {
        'conductance': conductance(graph, partition),
        'normalized_cut': normalized_cut(graph, partition),
        'modularity': modularity(graph, partition),
        'num_clusters': len(clusters),
        'num_nodes': len(partition),
        'avg_cluster_size': len(partition) / len(clusters) if clusters else 0,
        'cluster_sizes': {cid: len(nodes) for cid, nodes in clusters.items()}
    }