#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Сравнительный эксперимент алгоритмов сегментации VLAN.
"""

import sys
import os
from pathlib import Path

DE_PATH = Path(__file__).parent.parent / 'src' / 'Decision-Engine'
if DE_PATH.exists():
    sys.path.insert(0, str(DE_PATH))
else:
    DE_PATH = Path(__file__).parent.parent / 'src'
    sys.path.insert(0, str(DE_PATH))

import networkx as nx
import pandas as pd
import numpy as np
import time
import random
import math
import matplotlib.pyplot as plt
import seaborn as sns
from typing import Dict, List, Any
from collections import defaultdict
import warnings
warnings.filterwarnings('ignore')

try:
    from src.segmenters.greedy import GreedySegmenter
    from src.segmenters.simulated_annealing import SimulatedAnnealingSegmenter
    from src.segmenters.spectral import SpectralSegmenter
    from src.segmenters.interface import ConstraintSet
    REAL_SEGMENTERS = True
except ImportError:
    REAL_SEGMENTERS = False

def conductance(graph: nx.Graph, partition: Dict[int, int]) -> float:
    if not partition:
        return float('inf')
    clusters = defaultdict(list)
    for node, cluster_id in partition.items():
        clusters[cluster_id].append(node)
    if len(clusters) <= 1:
        return 0.0
    total = 0.0
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
            total += cut_edges / min(vol_cluster, vol_complement)
    return total / len(clusters) if clusters else float('inf')

def normalized_cut(graph: nx.Graph, partition: Dict[int, int]) -> float:
    if not partition:
        return float('inf')
    clusters = defaultdict(list)
    for node, cluster_id in partition.items():
        clusters[cluster_id].append(node)
    if len(clusters) <= 1:
        return 0.0
    total = 0.0
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
            total += cut_edges / vol_cluster + cut_edges / vol_complement
    return total / len(clusters) if clusters else float('inf')

def count_actions(partition: Dict[int, int], initial_partition: Dict[int, int]) -> int:
    actions = 0
    for node, target_vlan in partition.items():
        if initial_partition.get(node) != target_vlan:
            actions += 1
    return actions


def calculate_metrics(graph: nx.Graph, partition: Dict[int, int],
                      initial_partition: Dict[int, int] = None) -> Dict[str, Any]:
    if not partition:
        return {
            'conductance': float('inf'),
            'normalized_cut': float('inf'),
            'num_clusters': 0,
            'num_nodes': 0,
            'actions': 0
        }
    clusters = defaultdict(list)
    for node, cluster_id in partition.items():
        clusters[cluster_id].append(node)
    if initial_partition is None:
        initial_partition = {node: 1 for node in graph.nodes()}
    return {
        'conductance': conductance(graph, partition),
        'normalized_cut': normalized_cut(graph, partition),
        'num_clusters': len(clusters),
        'num_nodes': len(partition),
        'avg_cluster_size': len(partition) / len(clusters) if clusters else 0,
        'actions': count_actions(partition, initial_partition)
    }

class BuiltinGreedySegmenter:
    def __init__(self, max_iterations=100, lambda_coeff=0.5):
        self.max_iterations = max_iterations
        self.lambda_coeff = lambda_coeff

    def get_name(self):
        return "greedy"

    def segment(self, graph: nx.Graph, num_clusters: int, initial: Dict[int, int] = None) -> Dict[int, int]:
        nodes = list(graph.nodes())
        if not nodes:
            return {}
        if initial is None:
            segmentation = {}
            for i, node in enumerate(nodes):
                segmentation[node] = (i % max(1, num_clusters)) + 1
        else:
            segmentation = dict(initial)

        def objective(seg):
            intra = 0
            inter = 0
            for u, v, data in graph.edges(data=True):
                w = data.get('weight', 1.0)
                if seg.get(u) == seg.get(v):
                    intra += w
                else:
                    inter += w
            return intra - self.lambda_coeff * inter

        current_value = objective(segmentation)
        improvement = True
        iteration = 0

        while improvement and iteration < self.max_iterations:
            iteration += 1
            improvement = False
            best_delta = 0
            best_move = None
            for node in nodes:
                current_cluster = segmentation[node]
                for new_cluster in range(1, num_clusters + 1):
                    if new_cluster == current_cluster:
                        continue
                    test_seg = dict(segmentation)
                    test_seg[node] = new_cluster
                    new_value = objective(test_seg)
                    delta = new_value - current_value
                    if delta > best_delta:
                        best_delta = delta
                        best_move = (node, new_cluster)
            if best_move is not None and best_delta > 0.001:
                node, new_cluster = best_move
                segmentation[node] = new_cluster
                current_value += best_delta
                improvement = True
        return segmentation

class BuiltinSASegmenter:
    def __init__(self, initial_temperature=100.0, cooling_rate=0.95,
                 max_iterations=500, random_seed=42):
        self.initial_temperature = initial_temperature
        self.cooling_rate = cooling_rate
        self.max_iterations = max_iterations
        self.random_seed = random_seed

    def get_name(self):
        return "sa"

    def segment(self, graph: nx.Graph, num_clusters: int, initial: Dict[int, int] = None) -> Dict[int, int]:
        random.seed(self.random_seed)
        nodes = list(graph.nodes())
        if not nodes:
            return {}
        if initial is None:
            segmentation = {}
            for i, node in enumerate(nodes):
                segmentation[node] = (i % max(1, num_clusters)) + 1
        else:
            segmentation = dict(initial)

        def objective(seg):
            intra = 0
            inter = 0
            for u, v, data in graph.edges(data=True):
                w = data.get('weight', 1.0)
                if seg.get(u) == seg.get(v):
                    intra += w
                else:
                    inter += w
            return intra - 0.5 * inter

        current_value = objective(segmentation)
        best_seg = dict(segmentation)
        best_value = current_value
        temperature = self.initial_temperature

        for _ in range(self.max_iterations):
            if not nodes:
                break
            node = random.choice(nodes)
            current_cluster = segmentation[node]
            available = [c for c in range(1, num_clusters + 1) if c != current_cluster]
            if not available:
                continue
            new_cluster = random.choice(available)
            test_seg = dict(segmentation)
            test_seg[node] = new_cluster
            new_value = objective(test_seg)
            delta = new_value - current_value
            if delta > 0 or random.random() < math.exp(delta / max(temperature, 0.001)):
                segmentation[node] = new_cluster
                current_value = new_value
                if current_value > best_value:
                    best_seg = dict(segmentation)
                    best_value = current_value
            temperature *= self.cooling_rate
            if temperature < 0.001:
                break
        return best_seg

class BuiltinSpectralSegmenter:
    def __init__(self, random_state=42):
        self.random_state = random_state

    def get_name(self):
        return "spectral"

    def segment(self, graph: nx.Graph, num_clusters: int, initial: Dict[int, int] = None) -> Dict[int, int]:
        try:
            from sklearn.cluster import KMeans
            from scipy import linalg
        except ImportError:
            if initial is not None:
                return dict(initial)
            nodes = list(graph.nodes())
            segmentation = {}
            for i, node in enumerate(nodes):
                segmentation[node] = (i % max(1, num_clusters)) + 1
            return segmentation

        nodes = list(graph.nodes())
        n = len(nodes)
        if n == 0:
            return {}

        node_to_idx = {node: i for i, node in enumerate(nodes)}
        adj = np.zeros((n, n))
        for u, v, data in graph.edges(data=True):
            w = data.get('weight', 1.0)
            idx_u = node_to_idx.get(u)
            idx_v = node_to_idx.get(v)
            if idx_u is not None and idx_v is not None:
                adj[idx_u, idx_v] = w
                adj[idx_v, idx_u] = w

        degrees = np.sum(adj, axis=1)
        D = np.diag(degrees)
        D_inv_sqrt = np.diag(1.0 / np.sqrt(degrees + 1e-10))
        L = D_inv_sqrt @ (D - adj) @ D_inv_sqrt

        try:
            eigenvalues, eigenvectors = linalg.eigh(L)
        except:
            if initial is not None:
                return dict(initial)
            segmentation = {}
            for i, node in enumerate(nodes):
                segmentation[node] = (i % max(1, num_clusters)) + 1
            return segmentation

        k = min(num_clusters, n)
        if k <= 0:
            k = 1
        embedding = eigenvectors[:, 1:k+1]

        try:
            kmeans = KMeans(n_clusters=min(k, n), random_state=self.random_state, n_init=10)
            labels = kmeans.fit_predict(embedding)
        except:
            if initial is not None:
                return dict(initial)
            segmentation = {}
            for i, node in enumerate(nodes):
                segmentation[node] = (i % max(1, num_clusters)) + 1
            return segmentation

        segmentation = {}
        for i, node in enumerate(nodes):
            segmentation[node] = int(labels[i]) + 1
        return segmentation

def load_topology(filepath: str) -> nx.Graph:
    G = nx.Graph()
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                u = str(parts[0])
                v = str(parts[1])
                weight = float(parts[2]) if len(parts) >= 3 else 1.0
                G.add_edge(u, v, weight=weight)
    return G

def get_topology_info(filepath: str) -> Dict[str, Any]:
    G = load_topology(filepath)
    return {
        'nodes': G.number_of_nodes(),
        'edges': G.number_of_edges(),
        'graph': G
    }

def create_segmenter(segmenter_type: str, seed: int):
    if REAL_SEGMENTERS:
        if segmenter_type == "greedy":
            return GreedySegmenter(max_iterations=100, lambda_coeff=0.5)
        elif segmenter_type == "sa":
            return SimulatedAnnealingSegmenter(
                initial_temperature=100.0,
                cooling_rate=0.95,
                max_iterations=1000,
                random_seed=seed
            )
        elif segmenter_type == "spectral":
            return SpectralSegmenter(
                n_clusters=None,
                random_state=seed
            )
    else:
        if segmenter_type == "greedy":
            return BuiltinGreedySegmenter(max_iterations=100, lambda_coeff=0.5)
        elif segmenter_type == "sa":
            return BuiltinSASegmenter(
                initial_temperature=100.0,
                cooling_rate=0.95,
                max_iterations=500,
                random_seed=seed
            )
        elif segmenter_type == "spectral":
            return BuiltinSpectralSegmenter(random_state=seed)

def run_segmenter_on_graph(graph: nx.Graph, segmenter_type: str, num_clusters: int, seed: int, is_warmup: bool = False) -> Dict[str, Any]:
    random.seed(seed)
    np.random.seed(seed)

    nodes = list(graph.nodes())
    if not nodes:
        return {
            'segmenter_type': segmenter_type,
            'conductance': float('inf'),
            'normalized_cut': float('inf'),
            'num_clusters': 0,
            'num_nodes': 0,
            'actions': 0,
            'time_ms': 0.0,
            'seed': seed,
            'is_warmup': is_warmup
        }

    initial = {}
    for i, node in enumerate(nodes):
        initial[node] = (i % max(1, num_clusters)) + 1

    segmenter = create_segmenter(segmenter_type, seed)

    start_time = time.perf_counter()

    try:
        if REAL_SEGMENTERS:
            class MockModel:
                def __init__(self, graph):
                    self.graph = graph
                    self.state = None
                    self.metrics = []

                def evaluate(self, segmentation):
                    intra = 0
                    inter = 0
                    for u, v, data in graph.edges(data=True):
                        w = data.get('weight', 1.0)
                        if segmentation.get(u) == segmentation.get(v):
                            intra += w
                        else:
                            inter += w
                    return intra - 0.5 * inter

            model = MockModel(graph)
            constraints = ConstraintSet()
            result = segmenter.optimize(model, constraints, initial)
            segmentation = result.segmentation
        else:
            segmentation = segmenter.segment(graph, num_clusters, initial)
    except Exception as e:
        segmentation = initial

    elapsed_ms = (time.perf_counter() - start_time) * 1000
    metrics = calculate_metrics(graph, segmentation, initial)

    return {
        'segmenter_type': segmenter_type,
        'topology_size': len(nodes),
        'num_vlans_expected': num_clusters,
        'conductance': metrics['conductance'],
        'normalized_cut': metrics['normalized_cut'],
        'num_clusters_actual': metrics['num_clusters'],
        'num_nodes': metrics['num_nodes'],
        'actions': metrics['actions'],
        'time_ms': elapsed_ms,
        'seed': seed,
        'is_warmup': is_warmup
    }

def run_experiment(topologies: List[Dict], num_runs: int = 10) -> pd.DataFrame:
    segmenter_types = ['greedy', 'sa', 'spectral']
    seeds = list(range(1, num_runs + 1))
    all_results = []

    # Прогрев (без вывода)
    for topo_info in topologies:
        graph = topo_info['graph']
        num_clusters = topo_info['vlans']
        for seg_type in segmenter_types:
            run_segmenter_on_graph(graph, seg_type, num_clusters, seed=0, is_warmup=True)

    for topo_info in topologies:
        topo_name = topo_info['name']
        graph = topo_info['graph']
        num_clusters = topo_info['vlans']
        nodes = graph.number_of_nodes()

        print(f"Топология: {topo_name} (узлов={nodes}, VLAN={num_clusters})")

        for seg_type in segmenter_types:
            print(f"  Алгоритм: {seg_type}")
            for seed in seeds:
                print(f"    Прогон {seed}/{num_runs} (seed={seed})...", end="", flush=True)
                try:
                    result = run_segmenter_on_graph(graph, seg_type, num_clusters, seed, is_warmup=False)
                    result['topology'] = topo_name
                    all_results.append(result)
                    print(f" conductance={result['conductance']:.4f}, время={result['time_ms']:.2f}мс")
                except Exception as e:
                    print(f" ОШИБКА: {e}")

    return pd.DataFrame(all_results)

def setup_plot_style():
    plt.style.use('seaborn-v0_8-darkgrid')
    sns.set_palette("husl")
    plt.rcParams['figure.figsize'] = (12, 8)
    plt.rcParams['font.size'] = 11
    plt.rcParams['axes.labelsize'] = 12
    plt.rcParams['axes.titlesize'] = 14
    plt.rcParams['legend.fontsize'] = 10

def create_boxplot(df: pd.DataFrame, output_dir: Path) -> Path:
    fig, axes = plt.subplots(1, 3, figsize=(18, 6))
    topologies = df['topology'].unique()
    colors = {'greedy': '#2ecc71', 'sa': '#3498db', 'spectral': '#e74c3c'}

    for idx, topo in enumerate(topologies):
        topo_data = df[df['topology'] == topo]
        sns.boxplot(data=topo_data, x='segmenter_type', y='conductance',
                    palette=colors, ax=axes[idx])
        sns.swarmplot(data=topo_data, x='segmenter_type', y='conductance',
                      color='black', size=4, alpha=0.5, ax=axes[idx])
        axes[idx].set_title(f'{topo.capitalize()}')
        axes[idx].set_xlabel('Алгоритм')
        axes[idx].set_ylabel('Conductance (меньше = лучше)')
        axes[idx].grid(True, alpha=0.3)

    plt.tight_layout()
    path = output_dir / 'boxplot_conductance.png'
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    return path

def create_bar_chart(df: pd.DataFrame, output_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(12, 7))

    stats = df.groupby(['topology', 'segmenter_type'])['conductance'].agg(['mean', 'std']).reset_index()

    x = np.arange(len(stats['topology'].unique()))
    width = 0.25
    colors = {'greedy': '#2ecc71', 'sa': '#3498db', 'spectral': '#e74c3c'}

    for i, seg_type in enumerate(['greedy', 'sa', 'spectral']):
        seg_data = stats[stats['segmenter_type'] == seg_type]
        ax.bar(x + i*width, seg_data['mean'], width,
               label=seg_type, color=colors[seg_type],
               yerr=seg_data['std'], capsize=5)

    ax.set_xlabel('Топология')
    ax.set_ylabel('Средний Conductance (меньше = лучше)')
    ax.set_title('Сравнение Conductance по алгоритмам и топологиям')
    ax.set_xticks(x + width)
    ax.set_xticklabels(stats['topology'].unique())
    ax.legend(title='Алгоритм')
    ax.grid(True, alpha=0.3, axis='y')

    plt.tight_layout()
    path = output_dir / 'bar_chart_conductance.png'
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    return path

def create_line_chart(df: pd.DataFrame, output_dir: Path) -> Path:
    fig, ax = plt.subplots(figsize=(12, 7))

    stats = df.groupby(['topology', 'segmenter_type']).agg({
        'num_nodes': 'first',
        'time_ms': 'mean'
    }).reset_index()
    stats = stats.sort_values('num_nodes')

    colors = {'greedy': '#2ecc71', 'sa': '#3498db', 'spectral': '#e74c3c'}
    markers = {'greedy': 'o', 'sa': 's', 'spectral': '^'}

    for seg_type in ['greedy', 'sa', 'spectral']:
        seg_data = stats[stats['segmenter_type'] == seg_type]
        ax.plot(seg_data['num_nodes'], seg_data['time_ms'],
                marker=markers[seg_type], linewidth=2, markersize=10,
                label=seg_type, color=colors[seg_type])

    ax.set_xlabel('Число узлов в топологии')
    ax.set_ylabel('Среднее время выполнения (мс)')
    ax.set_title('Зависимость времени выполнения от размера топологии')
    ax.legend(title='Алгоритм')
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    path = output_dir / 'line_chart_time.png'
    plt.savefig(path, dpi=150, bbox_inches='tight')
    plt.close()
    return path

def save_to_excel_with_charts(df: pd.DataFrame, output_path: Path):
    try:
        from openpyxl import Workbook
        from openpyxl.chart import BarChart, Reference, Series, LineChart
        from openpyxl.chart.marker import Marker
        from openpyxl.utils.dataframe import dataframe_to_rows
    except ImportError:
        print("openpyxl не установлен. Выполните: pip install openpyxl")
        df.to_csv(output_path.with_suffix('.csv'), index=False)
        return

    print("Создание Excel с графиками...")

    wb = Workbook()

    ws1 = wb.active
    ws1.title = 'Data'
    for r in dataframe_to_rows(df, index=False, header=True):
        ws1.append(r)

    ws2 = wb.create_sheet('Summary')
    summary = df.groupby(['topology', 'segmenter_type']).agg({
        'conductance': ['mean', 'std', 'min', 'max'],
        'normalized_cut': ['mean', 'std'],
        'time_ms': ['mean', 'std'],
        'actions': ['mean', 'std'],
        'num_clusters_actual': ['mean']
    }).round(4)
    for r in dataframe_to_rows(summary, header=True):
        ws2.append(r)

    topologies = df['topology'].unique()
    algorithms = ['greedy', 'sa', 'spectral']

    ws_bar = wb.create_sheet('BarData')
    ws_bar.append(['Топология', 'Алгоритм', 'Средний Conductance', 'Стд. отклонение'])
    for topo in topologies:
        topo_data = df[df['topology'] == topo]
        for seg in algorithms:
            seg_data = topo_data[topo_data['segmenter_type'] == seg]
            ws_bar.append([topo, seg, seg_data['conductance'].mean(), seg_data['conductance'].std()])

    ws_line = wb.create_sheet('LineData')
    ws_line.append(['Топология', 'Алгоритм', 'Среднее время (мс)'])
    for seg in algorithms:
        seg_data = df[df['segmenter_type'] == seg].groupby('topology')['time_ms'].mean().reset_index()
        for _, row in seg_data.iterrows():
            ws_line.append([row['topology'], seg, row['time_ms']])

    ws_box = wb.create_sheet('BoxData')
    ws_box.append(['Алгоритм', 'Топология', 'Conductance'])
    for topo in topologies:
        for seg in algorithms:
            seg_data = df[(df['topology'] == topo) & (df['segmenter_type'] == seg)]['conductance'].tolist()
            for val in seg_data:
                ws_box.append([seg, topo, val])

    ws_charts = wb.create_sheet('Графики')

    chart1 = BarChart()
    chart1.title = "Средний Conductance по алгоритмам и топологиям"
    chart1.x_axis.title = "Топология"
    chart1.y_axis.title = "Средний Conductance (меньше = лучше)"
    chart1.legend.position = 'r'

    for i, seg in enumerate(algorithms):
        data_start = 2 + i * len(topologies)
        data_end = data_start + len(topologies) - 1
        categories = Reference(ws_bar, min_col=1, min_row=data_start, max_row=data_end)
        values = Reference(ws_bar, min_col=3, min_row=data_start, max_row=data_end)
        chart1.append(Series(values, categories, title=seg))

    chart1.width = 18
    chart1.height = 10
    ws_charts.add_chart(chart1, "A1")

    chart2 = LineChart()
    chart2.title = "Время выполнения vs размер топологии"
    chart2.x_axis.title = "Топология"
    chart2.y_axis.title = "Среднее время (мс)"
    chart2.legend.position = 'r'

    for i, seg in enumerate(algorithms):
        data_start = 2 + i * len(topologies)
        data_end = data_start + len(topologies) - 1
        categories = Reference(ws_line, min_col=1, min_row=data_start, max_row=data_end)
        values = Reference(ws_line, min_col=3, min_row=data_start, max_row=data_end)
        series = Series(values, categories, title=seg)
        series.marker = Marker('circle')
        chart2.append(series)

    chart2.width = 18
    chart2.height = 10
    ws_charts.add_chart(chart2, "A25")

    output_path = output_path.with_suffix('.xlsx')
    wb.save(str(output_path))
    print(f"Excel сохранён: {output_path}")

def main():
    print("Сравнительный эксперимент алгоритмов сегментации VLAN")

    data_dir = Path(__file__).parent.parent / 'data'
    if not data_dir.exists():
        print(f"Директория {data_dir} не найдена!")
        print("Сначала запустите gen_topology.py для генерации топологий.")
        return

    topologies = [
        {'name': 'small', 'path': data_dir / 'topology_small.edgelist', 'vlans': 3},
        {'name': 'medium', 'path': data_dir / 'topology_medium.edgelist', 'vlans': 8},
        {'name': 'large', 'path': data_dir / 'topology_large.edgelist', 'vlans': 15}
    ]

    topo_data = []
    for topo in topologies:
        if not topo['path'].exists():
            print(f"{topo['path']} не найден, пропускаем...")
            continue
        info = get_topology_info(str(topo['path']))
        topo_data.append({
            'name': topo['name'],
            'graph': info['graph'],
            'vlans': topo['vlans'],
            'nodes': info['nodes']
        })

    if not topo_data:
        print("Нет топологий для эксперимента!")
        return

    output_dir = Path(__file__).parent / 'results_final'
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\nЗапуск эксперимента...")
    df = run_experiment(topo_data, num_runs=10)

    df_results = df[df['is_warmup'] == False].copy()

    csv_path = output_dir / 'results_segmentation.csv'
    df_results.to_csv(csv_path, index=False)
    print(f"CSV сохранён: {csv_path}")

    csv_full_path = output_dir / 'results_segmentation_full.csv'
    df.to_csv(csv_full_path, index=False)
    print(f"Полный CSV с warmup сохранён: {csv_full_path}")

    excel_path = output_dir / 'results_segmentation.xlsx'
    save_to_excel_with_charts(df_results, excel_path)

    print("\nСоздание PNG графиков...")
    setup_plot_style()
    create_boxplot(df_results, output_dir)
    create_bar_chart(df_results, output_dir)
    create_line_chart(df_results, output_dir)
    print(f"PNG графики сохранены в {output_dir}/")

    summary = df_results.groupby(['topology', 'segmenter_type']).agg({
        'conductance': ['mean', 'std', 'min', 'max'],
        'normalized_cut': ['mean', 'std'],
        'time_ms': ['mean', 'std'],
        'actions': ['mean', 'std'],
        'num_clusters_actual': ['mean']
    }).round(4)
    summary.to_csv(output_dir / 'summary_table.csv')
    print(f"Сводная таблица сохранена: {output_dir / 'summary_table.csv'}")

    print("\n[СТАТИСТИКА]")

    for topo in df_results['topology'].unique():
        topo_data = df_results[df_results['topology'] == topo]
        print(f"\n{topo.capitalize()}:")
        for seg in ['greedy', 'sa', 'spectral']:
            seg_data = topo_data[topo_data['segmenter_type'] == seg]
            mean_cond = seg_data['conductance'].mean()
            std_cond = seg_data['conductance'].std()
            mean_time = seg_data['time_ms'].mean()
            print(f"  {seg}: conductance={mean_cond:.4f}+-{std_cond:.4f}, время={mean_time:.2f}мс")

    overall = df_results.groupby('segmenter_type').agg({
        'conductance': ['mean', 'std'],
        'time_ms': ['mean', 'std']
    }).round(4)
    print("\nОбщая статистика:")
    print(overall)

    print(f"\nРезультаты сохранены в: {output_dir}/")

if __name__ == "__main__":
    main()