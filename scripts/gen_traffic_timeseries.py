#!/usr/bin/env python
# -*- coding: utf-8 -*-

import numpy as np
import pandas as pd
import requests
import time
import json
from datetime import datetime, timedelta
import argparse
import random

class TrafficGenerator:
    def __init__(self, seed=42, vlan_ids=None):
        self.seed = seed
        np.random.seed(seed)
        random.seed(seed)

        if vlan_ids is None:
            self.vlan_ids = [10, 20, 30]
        else:
            self.vlan_ids = vlan_ids

        self.current_time = None
        self.data = {}

    def generate_normal_traffic(self, vlan_id, hours=24, interval_minutes=1):
        points = hours * 60 // interval_minutes
        time_points = []

        hour_phase = np.linspace(0, 2 * np.pi * (hours / 24), points)
        daily_pattern = 0.5 + 0.5 * np.sin(hour_phase - np.pi / 2)
        noise = np.random.normal(0, 0.15, points)
        base_rate = random.uniform(50, 200)
        traffic = base_rate * (daily_pattern + noise)
        traffic = np.maximum(traffic, 10)

        # Генерируем дополнительные метрики
        packets = traffic * 1000 / 1500  # примерное число пакетов
        active_src = 5 + np.random.poisson(3, points)  # активные источники
        icmp = np.random.poisson(2, points)  # ICMP пакеты

        self.data[vlan_id] = {
            'traffic': traffic,
            'packets': packets,
            'active_src': active_src,
            'icmp': icmp,
            'daily_pattern': daily_pattern,
            'base_rate': base_rate
        }
        return traffic

    def add_backup_spike(self, vlan_id, time_index, duration_minutes=30, spike_factor=3.0):
        if vlan_id not in self.data:
            return
        traffic = self.data[vlan_id]['traffic']
        packets = self.data[vlan_id]['packets']
        active_src = self.data[vlan_id]['active_src']

        backup_start = min(120, len(traffic) // 5)
        backup_end = min(backup_start + duration_minutes, len(traffic))

        for i in range(backup_start, backup_end):
            if i < len(traffic):
                progress = (i - backup_start) / duration_minutes if duration_minutes > 0 else 0
                spike = np.sin(np.pi * progress) * spike_factor
                traffic[i] = traffic[i] * (1 + spike)
                packets[i] = packets[i] * (1 + spike)
                active_src[i] = min(active_src[i] + 5, 30)

        self.data[vlan_id]['traffic'] = traffic
        self.data[vlan_id]['packets'] = packets
        self.data[vlan_id]['active_src'] = active_src
        self.data[vlan_id]['has_backup'] = True

    def add_legitimate_peak(self, vlan_id, time_index, duration_minutes=15, spike_factor=2.0):
        if vlan_id not in self.data:
            return
        traffic = self.data[vlan_id]['traffic']
        packets = self.data[vlan_id]['packets']
        active_src = self.data[vlan_id]['active_src']

        peak_start = min(len(traffic) // 2, len(traffic) - duration_minutes - 10)
        peak_end = min(peak_start + duration_minutes, len(traffic))

        for i in range(peak_start, peak_end):
            if i < len(traffic):
                progress = (i - peak_start) / duration_minutes if duration_minutes > 0 else 0
                spike = np.sin(np.pi * progress) * spike_factor
                traffic[i] = traffic[i] * (1 + spike)
                packets[i] = packets[i] * (1 + spike)
                active_src[i] = min(active_src[i] + 3, 25)

        self.data[vlan_id]['traffic'] = traffic
        self.data[vlan_id]['packets'] = packets
        self.data[vlan_id]['active_src'] = active_src
        self.data[vlan_id]['has_legitimate_peak'] = True

    def add_attack(self, vlan_id, time_index, attack_type='icmp_flood', duration_minutes=10, intensity=5.0):
        if vlan_id not in self.data:
            return
        traffic = self.data[vlan_id]['traffic']
        packets = self.data[vlan_id]['packets']
        active_src = self.data[vlan_id]['active_src']
        icmp = self.data[vlan_id]['icmp']
        flows = np.zeros(len(traffic))

        min_start = max(20, len(traffic) // 10)
        max_start = len(traffic) - duration_minutes - 20
        if max_start <= min_start:
            attack_start = len(traffic) // 3
        else:
            attack_start = random.randint(min_start, max_start)

        attack_end = min(attack_start + duration_minutes, len(traffic))

        for i in range(attack_start, attack_end):
            if i < len(traffic):
                if attack_type == 'icmp_flood':
                    # ICMP flood: резкий рост ICMP пакетов
                    traffic[i] = traffic[i] * intensity
                    packets[i] = packets[i] * intensity
                    icmp[i] = intensity * 1000
                    flows[i] = intensity * 100
                    active_src[i] = min(active_src[i] + 20, 50)
                elif attack_type == 'syn_scan':
                    # SYN scan: много мелких потоков
                    traffic[i] = traffic[i] * 0.1
                    packets[i] = packets[i] * 0.1
                    icmp[i] = 0
                    flows[i] = intensity * 500
                    active_src[i] = min(active_src[i] + 50, 100)
                elif attack_type == 'data_exfiltration':
                    # Утечка данных: большой объем
                    traffic[i] = traffic[i] * intensity * 2
                    packets[i] = packets[i] * intensity * 2
                    icmp[i] = 0
                    flows[i] = intensity * 50
                    active_src[i] = min(active_src[i] + 5, 30)

        self.data[vlan_id]['traffic'] = traffic
        self.data[vlan_id]['packets'] = packets
        self.data[vlan_id]['icmp'] = icmp
        self.data[vlan_id]['flows'] = flows
        self.data[vlan_id]['active_src'] = active_src
        self.data[vlan_id]['attack_type'] = attack_type
        self.data[vlan_id]['attack_start'] = attack_start
        self.data[vlan_id]['attack_end'] = attack_end

    def export_to_csv(self, filepath):
        all_data = []
        for vlan_id, data in self.data.items():
            traffic = data['traffic']
            flows = data.get('flows', np.zeros(len(traffic)))
            packets = data.get('packets', np.zeros(len(traffic)))
            active_src = data.get('active_src', np.zeros(len(traffic)))
            icmp = data.get('icmp', np.zeros(len(traffic)))
            
            for i, (t, f, p, a, ic) in enumerate(zip(traffic, flows, packets, active_src, icmp)):
                all_data.append({
                    'timestamp': i,
                    'vlan_id': vlan_id,
                    'traffic_mbps': t,
                    'flows_per_sec': f,
                    'packets_per_sec': p,
                    'active_src_ips': a,
                    'icmp_per_sec': ic,
                    'is_attack': 1 if 'attack_start' in data and data['attack_start'] <= i <= data['attack_end'] else 0
                })
        df = pd.DataFrame(all_data)
        df.to_csv(filepath, index=False)
        print(f"CSV сохранен: {filepath}")
        return df

    def send_to_victoriametrics(self, base_url="http://localhost:8428"):
        if not self.data:
            print("Нет данных для отправки")
            return False

        print(f"Отправка данных в VictoriaMetrics ({base_url})...")
        lines = []
        timestamp = int(time.time())

        for vlan_id, data in self.data.items():
            traffic = data['traffic']
            flows = data.get('flows', np.zeros(len(traffic)))
            packets = data.get('packets', np.zeros(len(traffic)))
            active_src = data.get('active_src', np.zeros(len(traffic)))
            icmp = data.get('icmp', np.zeros(len(traffic)))
            
            step = max(1, len(traffic) // 50)

            for i in range(0, len(traffic), step):
                ts = timestamp - (len(traffic) - i) * 60
                
                # netflow_bytes - основной трафик
                lines.append(
                    f'netflow_bytes{{vlan_id="{vlan_id}", direction="0"}} '
                    f'{traffic[i] * 1000000} {ts}'
                )
                
                # netflow_packets - пакеты
                lines.append(
                    f'netflow_packets{{vlan_id="{vlan_id}", direction="0"}} '
                    f'{packets[i]} {ts}'
                )
                
                # netflow_flow_count - количество потоков
                lines.append(
                    f'netflow_flow_count{{vlan_dst="{vlan_id}", direction="1"}} '
                    f'{flows[i]} {ts}'
                )
                
                # netflow_active_src_ips - активные источники
                lines.append(
                    f'netflow_active_src_ips{{vlan_id="{vlan_id}"}} '
                    f'{int(active_src[i])} {ts}'
                )
                
                # netflow_icmp_packets_total - ICMP пакеты
                lines.append(
                    f'netflow_icmp_packets_total{{vlan_id="{vlan_id}"}} '
                    f'{int(icmp[i])} {ts}'
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
    parser = argparse.ArgumentParser(description='Генератор временных рядов трафика')
    parser.add_argument('--output', '-o', default='../data/traffic.csv')
    parser.add_argument('--hours', type=int, default=24)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--vm-url', default='http://localhost:8428')
    parser.add_argument('--vlan-ids', default='10,20,30')

    args = parser.parse_args()
    vlan_ids = [int(x.strip()) for x in args.vlan_ids.split(',')]

    print("ГЕНЕРАЦИЯ ТЕСТОВОГО ТРАФИКА")

    gen = TrafficGenerator(seed=args.seed, vlan_ids=vlan_ids)

    print("Генерация нормального трафика...")
    for vlan_id in vlan_ids:
        gen.generate_normal_traffic(vlan_id, hours=args.hours)
        print(f"   VLAN {vlan_id}: сгенерирован")

    print("Добавление паттернов...")
    gen.add_backup_spike(10, None, duration_minutes=30, spike_factor=4.0)
    gen.add_backup_spike(20, None, duration_minutes=20, spike_factor=3.0)
    print("   VLAN 10,20: добавлено резервное копирование")

    gen.add_legitimate_peak(20, None, duration_minutes=15, spike_factor=2.5)
    gen.add_legitimate_peak(30, None, duration_minutes=20, spike_factor=2.0)
    print("   VLAN 20,30: добавлены легитимные пики")

    print("Добавление атак...")
    gen.add_attack(10, None, attack_type='icmp_flood', duration_minutes=8, intensity=6.0)
    print("   VLAN 10: ICMP flood атака")

    gen.add_attack(20, None, attack_type='syn_scan', duration_minutes=5, intensity=4.0)
    print("   VLAN 20: SYN scan атака")

    gen.add_attack(30, None, attack_type='data_exfiltration', duration_minutes=12, intensity=3.5)
    print("   VLAN 30: утечка данных")

    print(f"Экспорт в CSV: {args.output}")
    df = gen.export_to_csv(args.output)

    print("Статистика:")
    print(f"   Всего точек: {len(df)}")
    print(f"   VLAN: {df['vlan_id'].unique()}")
    print(f"   Атак: {df['is_attack'].sum()}")

    if args.send:
        gen.send_to_victoriametrics(args.vm_url)

    print("Генерация завершена!")

if __name__ == "__main__":
    main()
