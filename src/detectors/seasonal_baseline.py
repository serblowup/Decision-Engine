"""
Метод C: Z-оценка с сезонным baseline
Использует исторический baseline для данного часа суток
"""

import math
from typing import List, Dict, Any
from collections import defaultdict
from datetime import datetime
import numpy as np

from .base import BaseDetector


class SeasonalBaselineDetector(BaseDetector):
    """Seasonal Baseline детектор аномалий"""
    
    def __init__(
        self,
        history_days: int = 7,
        threshold: float = 3.0,
        min_history: int = 5
    ):
        """
        Args:
            history_days: количество дней истории
            threshold: порог z-оценки
            min_history: минимальное количество точек для расчета
        """
        self.history_days = history_days
        self.threshold = threshold
        self.min_history = min_history
        
        # История для каждого VLAN по часам
        # {vlan_id: {hour: [values]}}
        self._history: Dict[int, Dict[int, List[float]]] = defaultdict(
            lambda: defaultdict(list)
        )
        
        # Для хранения временных меток
        self._timestamps: Dict[int, List[int]] = defaultdict(list)
    
    def get_name(self) -> str:
        return "SeasonalBaseline"
    
    def should_trigger(self, metrics: List[Dict[str, Any]]) -> bool:
        if not metrics:
            return False
        
        triggered = False
        
        for metric in metrics:
            vlan_id = int(metric.get('vlan_id', 0))
            if vlan_id == 0:
                continue
            
            value = float(metric.get('flows_per_sec', 0.0))
            
            # Получаем час из временной метки (если есть)
            timestamp_str = metric.get('window_end', '')
            hour = self._get_hour(timestamp_str)
            
            # Сохраняем в историю
            self._history[vlan_id][hour].append(value)
            self._timestamps[vlan_id].append(hour)
            
            # Проверяем аномалию
            if self._check_anomaly(vlan_id, hour, value):
                triggered = True
        
        return triggered
    
    def _get_hour(self, timestamp_str: str) -> int:
        """Извлекает час из временной метки"""
        try:
            if timestamp_str:
                dt = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                return dt.hour
        except:
            pass
        # Если не удалось, используем текущий час
        return datetime.now().hour
    
    def _check_anomaly(self, vlan_id: int, hour: int, value: float) -> bool:
        """Проверяет аномалию для данного часа"""
        hour_values = self._history[vlan_id].get(hour, [])
        
        if len(hour_values) < self.min_history:
            return False
        
        # Вычисляем среднее и std для этого часа
        mean = np.mean(hour_values[:-1]) if len(hour_values) > 1 else np.mean(hour_values)
        std = np.std(hour_values[:-1]) if len(hour_values) > 1 else np.std(hour_values)
        
        if std < 1e-6:
            return False
        
        z_score = (value - mean) / std
        
        return z_score > self.threshold