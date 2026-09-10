#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Генератор топологий VLAN

import networkx as nx
import random
import csv
import json
from pathlib import Path
from datetime import datetime
import argparse
import matplotlib.pyplot as plt

class TopologyGenerator:
    def __init__(self, seed=42):
        self.seed = seed
        random.seed(seed)
        self.graph = None
        self.vlan_members = {}
        self.devices = []
        self.node_info = {}
        
    def generate_topology(
        self,
        total_nodes: int = 10,
        num_vlans: int = 3,
        topology_type: str = "small",
        name: str = ""
    ):
        """
        Генерация топологии с заданным количеством узлов и VLAN.
        """
        self.graph = nx.Graph()
        self.devices = []
        self.vlan_members = {vlan_id: [] for vlan_id in range(1, num_vlans + 1)}
        self.node_info = {}
        
        num_switches = num_vlans
        num_hosts = total_nodes - 1 - num_switches
        
        print(f"\n{name.upper()}:")
        print(f"   Узлов: {total_nodes} (роутер: 1, коммутаторы: {num_switches}, хосты: {num_hosts})")
        print(f"   VLAN: {num_vlans}")
        
        # Роутер
        router_id = "router-1"
        self.graph.add_node(router_id, device_type='ROUTER', level=0)
        self.devices.append({
            'id': router_id,
            'hostname': 'router-1',
            'mgmt_ip': '192.168.0.254',
            'device_type': 'ROUTER',
            'level': 0,
            'status': 'ACTIVE'
        })
        self.node_info[router_id] = {'type': 'ROUTER', 'level': 0}
        
        # Коммутаторы (по одному на VLAN)
        switch_ids = []
        for vlan_id in range(1, num_vlans + 1):
            sw_id = f"sw-{vlan_id}"
            switch_ids.append(sw_id)
            
            self.graph.add_node(sw_id, device_type='SWITCH', level=1)
            self.devices.append({
                'id': sw_id,
                'hostname': f'sw-{vlan_id}',
                'mgmt_ip': f'192.168.1.{vlan_id}',
                'device_type': 'SWITCH',
                'level': 1,
                'vlan': vlan_id,
                'status': 'ACTIVE'
            })
            self.node_info[sw_id] = {'type': 'SWITCH', 'level': 1, 'vlan': vlan_id}
            self.vlan_members[vlan_id].append(sw_id)
            self.graph.add_edge(router_id, sw_id, weight=1.0, link_type='router-switch')
        
        # Хосты (равномерно распределяем по VLAN)
        host_index = 0
        for vlan_id in range(1, num_vlans + 1):
            hosts_in_vlan = num_hosts // num_vlans
            if vlan_id <= num_hosts % num_vlans:
                hosts_in_vlan += 1
            
            sw_id = switch_ids[vlan_id - 1]
            
            for i in range(hosts_in_vlan):
                host_index += 1
                host_id = f"host-{host_index}"
                
                self.graph.add_node(host_id, device_type='HOST', level=2)
                self.devices.append({
                    'id': host_id,
                    'hostname': f'host-{host_index}',
                    'mgmt_ip': f'192.168.2.{host_index}',
                    'device_type': 'HOST',
                    'level': 2,
                    'vlan': vlan_id,
                    'parent_switch': sw_id,
                    'status': 'ACTIVE'
                })
                self.node_info[host_id] = {'type': 'HOST', 'level': 2, 'vlan': vlan_id}
                self.vlan_members[vlan_id].append(host_id)
                self.graph.add_edge(sw_id, host_id, weight=1.0, link_type='switch-host')
        
        # Добавляем случайные связи между коммутаторами
        for i in range(num_switches):
            for j in range(i + 1, num_switches):
                if random.random() < 0.2:
                    sw_i = switch_ids[i]
                    sw_j = switch_ids[j]
                    self.graph.add_edge(sw_i, sw_j, weight=1.0, link_type='switch-switch')
        
        # Метаданные
        self.graph.graph['topology_type'] = topology_type
        self.graph.graph['num_nodes'] = total_nodes
        self.graph.graph['num_switches'] = num_switches
        self.graph.graph['num_hosts'] = num_hosts
        self.graph.graph['num_vlans'] = num_vlans
        self.graph.graph['seed'] = self.seed
        self.graph.graph['generated_at'] = datetime.now().isoformat()
        
        self.vlan_members = {v: m for v, m in self.vlan_members.items() if m}
        
        return self.graph
    
    def generate_small(self):
        return self.generate_topology(
            total_nodes=10,
            num_vlans=3,
            topology_type="small",
            name="SMALL"
        )
    
    def generate_medium(self):
        return self.generate_topology(
            total_nodes=50,
            num_vlans=8,
            topology_type="medium",
            name="MEDIUM"
        )
    
    def generate_large(self):
        return self.generate_topology(
            total_nodes=100,
            num_vlans=15,
            topology_type="large",
            name="LARGE"
        )
    
    def export_edge_list(self, filepath):
        if self.graph is None:
            raise ValueError("Граф не сгенерирован")
        nx.write_edgelist(self.graph, filepath, data=['weight'])
        print(f"   Edge list: {filepath}")
    
    def export_json(self, filepath):
        if self.graph is None:
            raise ValueError("Граф не сгенерирован")
        
        data = {
            'devices': self.devices,
            'edges': list(self.graph.edges(data=True)),
            'vlan_members': self.vlan_members,
            'metadata': self.graph.graph
        }
        
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, default=str)
        print(f"   JSON: {filepath}")
    
    def export_vlan_assignments(self, filepath):
        if self.graph is None:
            raise ValueError("Граф не сгенерирован")
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['node_id', 'vlan_id', 'device_type', 'level'])
            for node_id, info in self.node_info.items():
                writer.writerow([
                    node_id,
                    info.get('vlan', 0),
                    info.get('type', 'UNKNOWN'),
                    info.get('level', -1)
                ])
        print(f"   Assignments: {filepath}")
    
    def export_devices(self, filepath):
        if not self.devices:
            raise ValueError("Нет устройств")
        
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            writer.writerow(['id', 'hostname', 'mgmt_ip', 'device_type', 'level', 'vlan', 'status'])
            for dev in self.devices:
                writer.writerow([
                    dev['id'],
                    dev['hostname'],
                    dev['mgmt_ip'],
                    dev['device_type'],
                    dev.get('level', -1),
                    dev.get('vlan', 0),
                    dev.get('status', 'ACTIVE')
                ])
        print(f"   Devices: {filepath}")
    
    def export_graphml(self, filepath):
        if self.graph is None:
            raise ValueError("Граф не сгенерирован")
        
        for node, data in self.graph.nodes(data=True):
            for key, value in list(data.items()):
                if value is None:
                    data[key] = ''
        
        nx.write_graphml(self.graph, filepath)
        print(f"   GraphML: {filepath}")
    
    def export_png(self, filepath, name=""):
        """Экспорт топологии в PNG"""
        if self.graph is None:
            raise ValueError("Граф не сгенерирован")
        
        plt.figure(figsize=(14, 10))
        
        # Цвета для разных типов устройств
        color_map = []
        node_labels = {}
        for node in self.graph.nodes():
            node_type = self.node_info.get(node, {}).get('type', 'UNKNOWN')
            if node_type == 'ROUTER':
                color_map.append('red')
            elif node_type == 'SWITCH':
                color_map.append('blue')
            elif node_type == 'HOST':
                color_map.append('green')
            else:
                color_map.append('gray')
            node_labels[node] = node
        
        # Позиции узлов
        pos = nx.spring_layout(self.graph, seed=self.seed, k=2, iterations=50)
        
        # Рисуем узлы
        nx.draw_networkx_nodes(self.graph, pos, node_color=color_map, node_size=1200, alpha=0.9, edgecolors='black', linewidths=2)
        
        # Рисуем рёбра с разными цветами для разных типов связей
        edge_colors = []
        for u, v, data in self.graph.edges(data=True):
            link_type = data.get('link_type', 'unknown')
            if link_type == 'router-switch':
                edge_colors.append('orange')
            elif link_type == 'switch-switch':
                edge_colors.append('purple')
            elif link_type == 'switch-host':
                edge_colors.append('gray')
            else:
                edge_colors.append('black')
        
        nx.draw_networkx_edges(self.graph, pos, edge_color=edge_colors, width=2, alpha=0.7)
        
        # Подписи
        nx.draw_networkx_labels(self.graph, pos, node_labels, font_size=10, font_weight='bold')
        
        # Легенда для узлов
        from matplotlib.patches import Patch
        legend_elements = [
            Patch(facecolor='red', edgecolor='black', label='ROUTER'),
            Patch(facecolor='blue', edgecolor='black', label='SWITCH'),
            Patch(facecolor='green', edgecolor='black', label='HOST'),
            Patch(facecolor='orange', edgecolor='orange', label='router-switch'),
            Patch(facecolor='purple', edgecolor='purple', label='switch-switch'),
            Patch(facecolor='gray', edgecolor='gray', label='switch-host')
        ]
        plt.legend(handles=legend_elements, loc='upper right', fontsize=10)
        
        # Заголовок
        device_counts = self.get_statistics()['device_types']
        plt.title(f"Топология {name.upper()}\nУзлов: {self.graph.number_of_nodes()}, Рёбер: {self.graph.number_of_edges()}, VLAN: {len(self.vlan_members)}\nROUTER: {device_counts.get('ROUTER', 0)}, SWITCH: {device_counts.get('SWITCH', 0)}, HOST: {device_counts.get('HOST', 0)}", fontsize=14)
        
        plt.tight_layout()
        plt.savefig(filepath, dpi=150, bbox_inches='tight')
        plt.close()
        print(f"   PNG: {filepath}")
    
    def get_statistics(self):
        if self.graph is None:
            return None
        
        device_types = {}
        for node_id, info in self.node_info.items():
            dtype = info.get('type', 'UNKNOWN')
            device_types[dtype] = device_types.get(dtype, 0) + 1
        
        edge_types = {}
        for u, v, data in self.graph.edges(data=True):
            etype = data.get('link_type', 'unknown')
            edge_types[etype] = edge_types.get(etype, 0) + 1
        
        return {
            'nodes': self.graph.number_of_nodes(),
            'edges': self.graph.number_of_edges(),
            'vlans': len(self.vlan_members),
            'density': nx.density(self.graph),
            'avg_degree': sum(dict(self.graph.degree()).values()) / self.graph.number_of_nodes(),
            'device_types': device_types,
            'edge_types': edge_types,
            'vlan_sizes': {v: len(m) for v, m in self.vlan_members.items()}
        }

def main():
    parser = argparse.ArgumentParser(description='Генератор топологий VLAN')
    parser.add_argument('--output', '-o', default='../data/topology',
                       help='Базовое имя выходных файлов')
    parser.add_argument('--seed', type=int, default=42,
                       help='Seed для воспроизводимости')
    
    args = parser.parse_args()
    
    gen = TopologyGenerator(seed=args.seed)
    
    topologies = [
        ('small', gen.generate_small),
        ('medium', gen.generate_medium),
        ('large', gen.generate_large),
    ]
    
    print("[Генерация топологий VLAN]")
    print(f"Seed: {args.seed}")
    print(f"Выходной каталог: {args.output}")
    
    for name, generator_func in topologies:
        G = generator_func()
        stats = gen.get_statistics()
        base_name = f"{args.output}_{name}"
        
        gen.export_edge_list(f"{base_name}.edgelist")
        gen.export_json(f"{base_name}.json")
        gen.export_vlan_assignments(f"{base_name}_assignments.csv")
        gen.export_devices(f"{base_name}_devices.csv")
        gen.export_graphml(f"{base_name}.graphml")
        gen.export_png(f"{base_name}.png", name)
    
    print("[Генерация завершена]")

if __name__ == "__main__":
    main()