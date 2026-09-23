"""Linux control application for the ATORCH BW600 electronic load, by SQ6EMM."""

from .device import BW600, DeviceError, find_devices

__all__ = ["BW600", "DeviceError", "find_devices"]
__version__ = "0.1.0"
