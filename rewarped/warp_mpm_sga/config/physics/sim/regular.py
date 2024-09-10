from dataclasses import dataclass
from .base import BaseSimConfig

@dataclass(kw_only=True)
class RegularSimConfig(BaseSimConfig, name='regular'):
    num_steps: int = 1000
    gravity: tuple[float, float, float] = (0.0, -9.8, 0.0)
    bc: str = 'dexdeform'
    num_grids: int = 32
    dt: float = 5e-4
    bound: int = 3
    clip_bound: float = 0.5
    eps: float = 1e-15
    skip_frames: int = 1

    lower_lim: tuple[float, float, float] = (0.0, 0.0, 0.0)
    upper_lim: tuple[float, float, float] = (1.0, 1.0, 1.0)

    body_friction: float = 0.5
    body_softness: float = 666.
    ground_friction: float = 500.
