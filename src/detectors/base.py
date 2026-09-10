from abc import ABC, abstractmethod
from typing import List, Dict, Any


class BaseDetector(ABC):
    """Базовый класс для детекторов аномалий"""
    
    @abstractmethod
    def should_trigger(self, metrics: List[Dict[str, Any]]) -> bool:
        """
        Проверяет, нужно ли сработать детектору
        
        Args:
            metrics: список метрик от VictoriaMetrics
            
        Returns:
            True если обнаружена аномалия
        """
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Возвращает имя детектора"""
        pass
    
    def update(self, metrics: List[Dict[str, Any]]) -> None:
        """Обновляет внутреннее состояние детектора (опционально)"""
        pass