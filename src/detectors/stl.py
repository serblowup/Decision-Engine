"""
Метод B: STL-декомпозиция (Seasonal-Trend using LOESS)
x(t) = T(t) + S(t) + R(t)
Аномалия детектируется по z-оценке остатка R(t)
"""

import math
from typing import List, Dict, Any
from collections import defaultdict
import numpy as np

try:
    from statsmodels.tsa.seasonal import STL
except ImportError:
    STL = None

from .base import BaseDetector


class STLDetector(BaseDetector):
    """STL детектор аномалий"""
    
    def __init__(
        self,
        period: int = 12,
        threshold: float = 2.5,
        min_window: int = 24,
        seasonal: int = 3
    ):
        """
        Args:
            period: период сезонности
            threshold: порог z-оценки
            min_window: минимальный размер окна
            seasonal: параметр сезонности для STL
        """
        self.period = period
        self.threshold = threshold
        self.min_window = min_window
        self.seasonal = seasonal
        
        # История для каждого VLAN
        self._history: Dict[int, List[float]] = defaultdict(list)
    
    def get_name(self) -> str:
        return "STL"
    
    def should_trigger(self, metrics: List[Dict[str, Any]]) -> bool:
        if STL is None:
            return self._fallback_zscore(metrics)
        
        if not metrics:
            return False
        
        triggered = False
        
        for metric in metrics:
            vlan_id = int(metric.get('vlan_id', 0))
            if vlan_id == 0:
                continue
            
            value = float(metric.get('flows_per_sec', 0.0))
            self._history[vlan_id].append(value)
            
            if len(self._history[vlan_id]) < self.min_window:
                continue
            
            if self._check_anomaly(vlan_id):
                triggered = True
        
        return triggered
    
    def _check_anomaly(self, vlan_id: int) -> bool:
        """Проверяет аномалию с помощью STL"""
        try:
            data = np.array(self._history[vlan_id])
            
            # Проверяем, что данных достаточно
            if len(data) < self.min_window:
                return False
            
            # STL-декомпозиция
            stl = STL(data, period=self.period, seasonal=self.seasonal)
            result = stl.fit()
            
            # Берем остаток
            residual = result.resid
            
            # Проверяем последнее значение остатка
            if len(residual) < 2:
                return False
            
            last_residual = residual[-1]
            
            # Проверяем, не NaN ли значение
            if np.isnan(last_residual):
                return False
            
            # Вычисляем z-оценку для последнего остатка
            # Используем все остатки для статистики, кроме последнего
            residual_mean = np.nanmean(residual[:-1])
            residual_std = np.nanstd(residual[:-1])
            
            if residual_std < 1e-6:
                return False
            
            z_score = (last_residual - residual_mean) / residual_std
            
            return z_score > self.threshold
            
        except Exception as e:
            # Если STL не работает, используем простую z-оценку
            return self._simple_zscore(vlan_id)
    
    def _simple_zscore(self, vlan_id: int) -> bool:
        """Простая z-оценка как fallback"""
        data = self._history[vlan_id]
        if len(data) < self.min_window:
            return False
        
        last = data[-1]
        mean = np.mean(data[:-1])
        std = np.std(data[:-1])
        
        if std < 1e-6:
            return False
        
        z_score = (last - mean) / std
        return z_score > self.threshold
    
    def _fallback_zscore(self, metrics: List[Dict[str, Any]]) -> bool:
        """Fallback: простая z-оценка если statsmodels не установлен"""
        history = defaultdict(list)
        
        for metric in metrics:
            vlan_id = int(metric.get('vlan_id', 0))
            if vlan_id == 0:
                continue
            
            value = float(metric.get('flows_per_sec', 0.0))
            history[vlan_id].append(value)
            
            if len(history[vlan_id]) < self.min_window:
                continue
            
            data = history[vlan_id]
            last = data[-1]
            mean = np.mean(data[:-1])
            std = np.std(data[:-1])
            
            if std < 1e-6:
                continue
            
            z_score = (last - mean) / std
            if z_score > self.threshold:
                return True
        
        return False