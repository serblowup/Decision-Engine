#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Генерация сценариев для оценки FPR

import numpy as np
import pandas as pd
import requests
import time
import random
from datetime import datetime
import argparse

class TrafficScenarioGenerator:
    def __init__(self, seed=42):
        self.seed = seed
        np.random.seed(seed)
        random.seed(seed)
        self.scenarios = {}
    
    def generate_backup_scenario(self, duration_minutes=30, base_traffic_mbps=100, backup_traffic_mbps=800):
        points = duration_minutes * 60
        traffic = np.zeros(points)
        
        base = np.random.normal(base_traffic_mbps, 10, points)
        base = np.maximum(base, 50)
        
        for i in range(points):
            if i < points // 6:
                progress = i / (points // 6)
                traffic[i] = base[i] + backup_traffic_mbps * progress
            elif i < points * 5 // 6:
                traffic[i] = base[i] + backup_traffic_mbps
            else:
                progress = (points - i) / (points // 6)
                traffic[i] = base[i] + backup_traffic_mbps * progress
        
        self.scenarios['backup'] = {
            'traffic': traffic,
            'packets': traffic * 1000 / 1500,
            'flows': traffic * 10,
            'active_src': 5 + np.random.poisson(3, points),
            'duration_minutes': duration_minutes,
            'description': f'Резервное копирование: {backup_traffic_mbps} Мбит/с, {duration_minutes} мин'
        }
        return self.scenarios['backup']
    
    def generate_update_scenario(self, duration_minutes=20, base_traffic_mbps=150, peak_traffic_mbps=600):
        points = duration_minutes * 60
        traffic = np.zeros(points)
        
        base = np.random.normal(base_traffic_mbps, 20, points)
        base = np.maximum(base, 80)
        
        num_hosts = random.randint(20, 30)
        for host in range(num_hosts):
            start = random.randint(0, points // 2)
            duration = random.randint(60, 180)
            end = min(start + duration, points)
            intensity = random.uniform(0.5, 1.5) * peak_traffic_mbps / num_hosts
            
            for i in range(start, end):
                if i < points:
                    progress = (i - start) / duration
                    spike = np.sin(np.pi * progress)
                    traffic[i] += intensity * spike
        
        traffic = traffic + base
        
        self.scenarios['update'] = {
            'traffic': traffic,
            'packets': traffic * 1000 / 1500,
            'flows': traffic * 8,
            'active_src': 20 + np.random.poisson(5, points),
            'duration_minutes': duration_minutes,
            'description': f'Обновление ПО: {num_hosts} хостов, {duration_minutes} мин'
        }
        return self.scenarios['update']
    
    def generate_video_conference_scenario(self, duration_minutes=45, base_traffic_mbps=80, conference_traffic_mbps=200):
        points = duration_minutes * 60
        traffic = np.zeros(points)
        
        base = np.random.normal(base_traffic_mbps, 15, points)
        base = np.maximum(base, 40)
        
        num_streams = random.randint(3, 8)
        for stream in range(num_streams):
            start = random.randint(0, points // 4)
            duration = random.randint(points // 2, points)
            end = min(start + duration, points)
            bitrate = random.uniform(20, 50)
            
            for i in range(start, end):
                if i < points:
                    variation = 1 + 0.1 * np.sin(i / 100)
                    traffic[i] += bitrate * variation
        
        traffic = traffic + base
        
        self.scenarios['video_conference'] = {
            'traffic': traffic,
            'packets': traffic * 1000 / 1500,
            'flows': traffic * 5,
            'active_src': 10 + np.random.poisson(4, points),
            'duration_minutes': duration_minutes,
            'description': f'Видеоконференция: {num_streams} потоков, {duration_minutes} мин'
        }
        return self.scenarios['video_conference']
    
    def generate_attack_scenario(self, duration_minutes=5, base_traffic_mbps=50, attack_traffic_mbps=15000):
        """Сценарий: Атака (очень высокая интенсивность)"""
        points = duration_minutes * 60
        traffic = np.zeros(points)
        flows = np.zeros(points)
        
        base = np.random.normal(base_traffic_mbps, 10, points)
        base = np.maximum(base, 20)
        
        # Атака длится почти всё время (кроме первых 30 секунд)
        attack_start = 30
        attack_end = points - 10
        
        for i in range(points):
            if attack_start <= i < attack_end:
                traffic[i] = base[i] + attack_traffic_mbps
                flows[i] = (base[i] + attack_traffic_mbps) * 20
            else:
                traffic[i] = base[i]
                flows[i] = base[i] * 5
        
        self.scenarios['attack'] = {
            'traffic': traffic,
            'packets': traffic * 1000 / 1500,
            'flows': flows,
            'active_src': 80 + np.random.poisson(20, points),
            'duration_minutes': duration_minutes,
            'description': f'Атака: {attack_traffic_mbps} Мбит/с, {duration_minutes} мин (почти всё время)'
        }
        return self.scenarios['attack']
    
    def send_to_victoriametrics(self, base_url="http://localhost:8428"):
        if not self.scenarios:
            print("Нет данных для отправки")
            return False
        
        print(f"Отправка данных в VictoriaMetrics ({base_url})...")
        lines = []
        timestamp = int(time.time())
        
        for scenario_name, data in self.scenarios.items():
            traffic = data['traffic']
            packets = data['packets']
            flows = data['flows']
            active_src = data['active_src']
            
            step = max(1, len(traffic) // 50)
            
            for i in range(0, len(traffic), step):
                ts = timestamp - (len(traffic) - i)
                
                lines.append(
                    f'netflow_bytes{{vlan_id="10", direction="0", scenario="{scenario_name}"}} '
                    f'{traffic[i] * 1000000} {ts}'
                )
                lines.append(
                    f'netflow_packets{{vlan_id="10", direction="0", scenario="{scenario_name}"}} '
                    f'{packets[i]} {ts}'
                )
                lines.append(
                    f'netflow_flow_count{{vlan_dst="10", direction="1", scenario="{scenario_name}"}} '
                    f'{flows[i]} {ts}'
                )
                lines.append(
                    f'netflow_active_src_ips{{vlan_id="10", scenario="{scenario_name}"}} '
                    f'{int(active_src[i])} {ts}'
                )
        
        payload = "\n".join(lines) + "\n"
        try:
            response = requests.post(
                f"{base_url}/api/v1/import/prometheus",
                data=payload,
                headers={"Content-Type": "text/plain; version=0.0.4"},
                timeout=30
            )
            print(f"Отправлено {len(lines)} метрик, статус: {response.status_code}")
            return True
        except Exception as e:
            print(f"Ошибка отправки: {e}")
            return False

def main():
    parser = argparse.ArgumentParser(description='Генератор сценариев для оценки FPR')
    parser.add_argument('--send', action='store_true', help='Отправить данные в VictoriaMetrics')
    parser.add_argument('--vm-url', default='http://localhost:8428', help='URL VictoriaMetrics')
    parser.add_argument('--seed', type=int, default=42, help='Seed для воспроизводимости')
    
    args = parser.parse_args()
    
    print("ГЕНЕРАЦИЯ СЦЕНАРИЕВ ДЛЯ ОЦЕНКИ FPR")
    
    gen = TrafficScenarioGenerator(seed=args.seed)
    
    print("\n1. Генерация сценария: Резервное копирование")
    gen.generate_backup_scenario()
    
    print("\n2. Генерация сценария: Обновление ПО")
    gen.generate_update_scenario()
    
    print("\n3. Генерация сценария: Видеоконференция")
    gen.generate_video_conference_scenario()
    
    print("\n4. Генерация сценария: Атака (очень интенсивная)")
    gen.generate_attack_scenario()
    
    print("\nСтатистика по сценариям:")
    for name, data in gen.scenarios.items():
        traffic = data['traffic']
        flows = data['flows']
        print(f"  {name}:")
        print(f"    Средний трафик: {np.mean(traffic):.1f} Мбит/с")
        print(f"    Пиковый трафик: {np.max(traffic):.1f} Мбит/с")
        print(f"    Средние потоки: {np.mean(flows):.1f}")
        print(f"    Пиковые потоки: {np.max(flows):.1f}")
        print(f"    Продолжительность: {data['duration_minutes']} мин")
        print(f"    Активные источники (средние): {np.mean(data['active_src']):.1f}")
    
    if args.send:
        gen.send_to_victoriametrics(args.vm_url)
    
    print("Генерация сценариев завершена!")

if __name__ == "__main__":
    main()