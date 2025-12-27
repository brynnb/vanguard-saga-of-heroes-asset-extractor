"""
Shared data types for UE2 parsing.

These are common dataclasses used across multiple extractors.
"""

from dataclasses import dataclass


@dataclass
class Vector:
    """3D Vector."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0


@dataclass 
class Plane:
    """3D Plane (normal + distance)."""
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    w: float = 0.0
