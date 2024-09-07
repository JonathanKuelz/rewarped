from dataclasses import dataclass
from pathlib import Path
from .base import BasePhysicsConfig


@dataclass(kw_only=True)
class WaterPhysicsConfig(BasePhysicsConfig, name='water'):
    path: str = str(Path(__file__).parent.resolve() / 'templates' / 'water.py')
    elasticity: str = 'sigma'
    material: str = 'water'
    E: float = 1e5
    nu: float = 0.3
