"""
Unit-тесты для детекторов аномалий
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import pytest
import numpy as np
from src.detectors import EWMADetector, STLDetector, SeasonalBaselineDetector


def generate_sine_wave(length: int, amplitude: float = 100, period: int = 24) -> list:
    """Генерирует синусоидальный ряд"""
    return [amplitude * (1 + 0.5 * np.sin(2 * np.pi * i / period)) for i in range(length)]


def generate_with_attack(length: int, attack_start: int = 50, attack_intensity: float = 3.0) -> list:
    """Генерирует ряд с атакой"""
    data = generate_sine_wave(length)
    for i in range(attack_start, attack_start + 5):
        if i < len(data):
            data[i] *= attack_intensity
    return data


def generate_with_legitimate_peak(length: int, peak_start: int = 30, peak_factor: float = 1.5) -> list:
    """Генерирует ряд с легитимным пиком"""
    data = generate_sine_wave(length)
    for i in range(peak_start, peak_start + 10):
        if i < len(data):
            data[i] *= peak_factor
    return data


def create_metrics(data: list, vlan_id: int = 10) -> list:
    """Создает список метрик из данных"""
    return [{'vlan_id': vlan_id, 'flows_per_sec': v} for v in data]


class TestEWMADetector:
    
    def test_should_trigger_on_attack(self):
        """Тест: атака должна быть обнаружена"""
        data = generate_with_attack(100, attack_start=50, attack_intensity=3.0)
        metrics = create_metrics(data)
        
        detector = EWMADetector(alpha=0.3, threshold=2.5, min_window=5)
        
        # Прогоняем все метрики
        for i in range(5, len(metrics)):
            detector.should_trigger(metrics[i:i+1])
        
        # Проверяем срабатывание
        triggered = detector.should_trigger(metrics[50:55])
        assert triggered, "EWMA не обнаружил атаку"
    
    def test_should_not_trigger_on_legitimate_peak(self):
        """Тест: легитимный пик не должен быть обнаружен"""
        data = generate_with_legitimate_peak(100, peak_start=30, peak_factor=1.5)
        metrics = create_metrics(data)
        
        detector = EWMADetector(alpha=0.3, threshold=2.5, min_window=5)
        
        # Прогоняем все метрики
        for i in range(5, len(metrics)):
            detector.should_trigger(metrics[i:i+1])
        
        # Проверяем, что нет срабатывания
        triggered = detector.should_trigger(metrics[30:40])
        assert not triggered, "EWMA обнаружил легитимный пик как аномалию"


class TestSTLDetector:
    
    def test_should_trigger_on_attack(self):
        """Тест: атака должна быть обнаружена"""
        data = generate_with_attack(200, attack_start=100, attack_intensity=3.0)
        metrics = create_metrics(data)
        
        detector = STLDetector(period=12, threshold=2.5, min_window=24, seasonal=3)
        
        # Прогоняем все метрики
        for i in range(24, len(metrics)):
            detector.should_trigger(metrics[i:i+1])
        
        # Проверяем срабатывание
        triggered = detector.should_trigger(metrics[100:105])
        assert triggered, "STL не обнаружил атаку"
    
    def test_should_not_trigger_on_legitimate_peak(self):
        """Тест: легитимный пик не должен быть обнаружен"""
        data = generate_with_legitimate_peak(200, peak_start=60, peak_factor=1.5)
        metrics = create_metrics(data)
        
        detector = STLDetector(period=12, threshold=2.5, min_window=24, seasonal=3)
        
        # Прогоняем все метрики
        for i in range(24, len(metrics)):
            detector.should_trigger(metrics[i:i+1])
        
        # Проверяем, что нет срабатывания
        triggered = detector.should_trigger(metrics[60:70])
        assert not triggered, "STL обнаружил легитимный пик как аномалию"


class TestSeasonalBaselineDetector:
    
    def test_should_trigger_on_attack(self):
        """Тест: атака должна быть обнаружена"""
        # Генерируем несколько дней данных
        data = []
        for day in range(3):
            day_data = generate_sine_wave(24, amplitude=100, period=24)
            data.extend(day_data)
        
        # Добавляем атаку в конце 2-го дня
        attack_start = 24 * 2 + 12
        for i in range(attack_start, attack_start + 5):
            if i < len(data):
                data[i] *= 3.0
        
        metrics = create_metrics(data)
        
        detector = SeasonalBaselineDetector(history_days=3, threshold=2.5, min_history=2)
        
        # Прогоняем все метрики
        for i in range(len(metrics)):
            detector.should_trigger(metrics[i:i+1])
        
        # Проверяем срабатывание
        triggered = detector.should_trigger(metrics[attack_start:attack_start+5])
        assert triggered, "SeasonalBaseline не обнаружил атаку"
    
    def test_should_not_trigger_on_legitimate_peak(self):
        """Тест: легитимный пик не должен быть обнаружен"""
        # Генерируем несколько дней данных
        data = []
        for day in range(3):
            day_data = generate_sine_wave(24, amplitude=100, period=24)
            data.extend(day_data)
        
        # Добавляем легитимный пик в конце 2-го дня
        peak_start = 24 * 2 + 10
        for i in range(peak_start, peak_start + 10):
            if i < len(data):
                data[i] *= 1.5
        
        metrics = create_metrics(data)
        
        detector = SeasonalBaselineDetector(history_days=3, threshold=2.5, min_history=2)
        
        # Прогоняем все метрики
        for i in range(len(metrics)):
            detector.should_trigger(metrics[i:i+1])
        
        # Проверяем, что нет срабатывания
        triggered = detector.should_trigger(metrics[peak_start:peak_start+10])
        assert not triggered, "SeasonalBaseline обнаружил легитимный пик как аномалию"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])