"""
Метод A: EWMA (Exponentially Weighted Moving Average)
μ̂(t) = α * x(t) + (1 - α) * μ̂(t-1)
"""

import math
from typing import List, Dict, Any
from collections import defaultdict
from .base import BaseDetector


class EWMADetector(BaseDetector):
    """EWMA детектор аномалий"""
    
    def __init__(self, alpha: float = 0.5, threshold: float = 2.0, min_window: int = 5):
        """
        Args:
            alpha: коэффициент сглаживания (0 < alpha < 1)
            threshold: порог z-оценки
            min_window: минимальный размер окна для расчета
        """
        self.alpha = alpha
        self.threshold = threshold
        self.min_window = min_window
        
        # Состояние для каждого VLAN
        self._mu: Dict[int, float] = {}
        self._sigma2: Dict[int, float] = {}  # дисперсия
        self._count: Dict[int, int] = {}
        self._history: Dict[int, List[float]] = defaultdict(list)
    
    def get_name(self) -> str:
        return "EWMA"
    
    def should_trigger(self, metrics: List[Dict[str, Any]]) -> bool:
        if not metrics:
            return False
        
        triggered = False
        
        for metric in metrics:
            vlan_id = int(metric.get('vlan_id', 0))
            if vlan_id == 0:
                continue
            
            value = float(metric.get('flows_per_sec', 0.0))
            self._history[vlan_id].append(value)
            
            # Обновляем EWMA
            if self._update_and_check(vlan_id, value):
                triggered = True
        
        return triggered
    
    def _update_and_check(self, vlan_id: int, value: float) -> bool:
        """Обновляет EWMA и проверяет аномалию"""
        if vlan_id not in self._mu:
            self._mu[vlan_id] = value
            self._sigma2[vlan_id] = 0.0
            self._count[vlan_id] = 1
            return False
        
        old_mu = self._mu[vlan_id]
        
        # Обновляем среднее
        self._mu[vlan_id] = self.alpha * value + (1 - self.alpha) * old_mu
        
        # Обновляем дисперсию (используем формулу для EWMA дисперсии)
        diff = value - self._mu[vlan_id]
        old_sigma2 = self._sigma2[vlan_id]
        self._sigma2[vlan_id] = (1 - self.alpha) * old_sigma2 + self.alpha * (diff ** 2)
        
        self._count[vlan_id] += 1
        
        # Проверяем аномалию
        if self._count[vlan_id] < self.min_window:
            return False
        
        sigma = math.sqrt(self._sigma2[vlan_id])
        if sigma < 1e-6:
            return False
        
        # Используем текущее значение для z-оценки
        z_score = (value - old_mu) / sigma
        
        return z_score > self.threshold